/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "ffn_fe100_punt.h"
#include <string.h>
static uint16_t be16(const uint8_t *p) {return (uint16_t)p[0]<<8|p[1];}
static uint32_t be32(const uint8_t *p) {return (uint32_t)be16(p)<<16|be16(p+2);}

int ffn_fe100_punt_decode(const uint8_t *wire,size_t size,
    const struct ffn_fe100_punt_scope *scope,struct ffn_fe100_punt *out)
{
    const uint8_t *cmh,*key=NULL,*info,*frame,*ip,*transport;
    size_t length,l3=14,l4,ip_length,overhead;
    uint16_t port,zone,lif,ether_type,packet_type;
    unsigned protocol;
    if(out)memset(out,0,sizeof(*out));
    if(!wire || !scope || !out || scope->front<1 || scope->front>63 ||
       scope->trunk==scope->return_port || size<40+34 || size>80+9216)return 0;
    /* Outer OTMH source is the internal FE100 return, not a front port.
     * RAW return retains the four-byte ITMH before the common message header.
     */
    if(be16(wire)!=scope->trunk || be16(wire+2)!=scope->return_port ||
       wire[4]!=1 || (wire[5]&0xe0) || wire[7])return 0;
    cmh=wire+8;
    /* FLOWUNKNOWN includes the IPv4 key. Qualified exception messages omit
     * that key and retain the original Ethernet packet (before NAT/TTL edits).
     * Never interpret control/status or rewritten forwarding messages as data.
     */
    if(cmh[0]!=0x10 || cmh[1] || (cmh[3]!=1 && cmh[3]!=2 && cmh[22]))return 0;
    if(cmh[3]==8 && !cmh[23]) {
        key=cmh+24;
        if(key[0]!=0x40 && key[0]!=0x80)return 0;
        overhead=key[0]==0x40 ? 56 : 80;
    }
    /* Exceptions (types 4 and 5) carry the original packet whatever their code:
     * the PA-5220 answered bare TCP SYNs with code 39 (2026-10-20), and a code
     * this decoder has not met must not cost the packet on the inline path. */
    else if(cmh[3]==4 || cmh[3]==5) overhead=40;
    /* NOTFLOW's eight-byte message info ends in a zone, not an exception
     * code. Confirmed against condor_notflow_msg_t and physical LACP returns. */
    else if(cmh[3]==1 && !be32(cmh+16) && !be16(cmh+20))overhead=40;
    /* A session the chip carries hands its TCP control segments (FIN, RST) to
     * the CPU as type 2: flow ID, then a zone in place of an exception code,
     * and the ORIGINAL packet before any rewrite (PA-5220 run 9, 2026-10-19:
     * DMAC still the appliance's, source still the LAN host). The kernel path
     * must see them so conntrack leaves ESTABLISHED and the lease is withdrawn. */
    else if(cmh[3]==2 && be32(cmh+16) && !be16(cmh+20))overhead=40;
    else return 0;
    if(size<overhead+34)return 0;
    info=wire+overhead-8;frame=wire+overhead;
    port=(be16(info)>>6)&63;lif=be16(info+4);
    zone=key ? be16(key+2) : (cmh[3]==1 || cmh[3]==2) ? be16(cmh+22) : scope->zone;
    length=be16(info+2)&0x3fff;
    packet_type=be16(info+2)>>14;
    if(be16(info)>>12 || port!=scope->front ||
       lif!=scope->in_lif || zone!=scope->zone || length<34 ||
       length>9216 || size!=overhead+length)return 0;
    ether_type=be16(frame+12);
    /* The commissioned VLAN-present metadata bit describes the original
     * Ethernet tag; it is not an unsupported extra-header flag. Both views
     * must agree, so a malformed return cannot select a different TAP unit. */
    if((ether_type==0x8100)!=!!(info[6]&0x80))return 0;
    if(ether_type==0x8100) {
        if(length<38 || (be16(frame+14)&0xfff)==0xfff)return 0;
        l3=18;ether_type=be16(frame+16);
    }
    if((info[7]&0x7f)!=l3)return 0;
    if(ether_type==0x8809 || ether_type==0x88cc) {
        static const uint8_t slow[6]={1,0x80,0xc2,0,0,2};
        static const uint8_t lldp[6]={1,0x80,0xc2,0,0,0x0e};
        if(packet_type || key || l3!=14 || ((be16(info+6)>>7)&0xff))return 0;
        if(ether_type==0x8809) {
            if(cmh[3]!=1 || memcmp(frame,slow,6) || length<124 || frame[14]!=1 || frame[15]!=1)return 0;
        } else if(cmh[3]!=4 || cmh[23]!=32 || memcmp(frame,lldp,6))return 0;
        *out=(struct ffn_fe100_punt){.frame=frame,.length=length,
            .front=port,.in_lif=lif,.zone=zone,.message=cmh[3],.code=cmh[3]==1?0:cmh[23]};
        return 2; /* Dedicated control receiver only; never a data TAP. */
    }
    /* NOTFLOW for anything but LACP/LLDP is an original frame the chip could not
     * classify as a flow (GRE, ESP, IGMP, PPPoE, MPLS, 802.1X ...): the inline
     * ingress must hand it to the kernel path like any other frame, or steering
     * the production ports into the chip would silently drop those protocols.
     * Envelope, scope, length and tag agreement were verified above. */
    if(cmh[3]==1)goto accepted;
    /* Type 2 carries the TCP segments of a known IPv4 flow; it has no key, so
     * the IPv6 view (which needs one) and everything else stay rejected. */
    if(cmh[3]==2 && ether_type!=0x0800)return 0;
    if(ether_type==0x0806) {
        if(packet_type || key || cmh[3]!=4 || cmh[23]!=32 || length<l3+28 ||
           ((be16(info+6)>>7)&0xff) || be16(frame+l3)!=1 || be16(frame+l3+2)!=0x0800 ||
           frame[l3+4]!=6 || frame[l3+5]!=4)return 0;
        goto accepted;
    }
    if(ether_type==0x86dd) {
        if(packet_type!=2 || length<l3+48)return 0;
        ip=frame+l3;protocol=ip[6];ip_length=40+be16(ip+4);l4=l3+40;
        if(ip[0]>>4!=6 || ip_length!=length-l3 || ((be16(info+6)>>7)&0xff)!=40)return 0;
        if(!key) {
            /* An exception without a key carries the original packet of any
             * next header; the kernel path decides what it is. */
            if(cmh[3]!=4 && cmh[3]!=5)return 0;
            goto accepted;
        }
        /* A keyed miss must name this packet's next header and addresses; its
         * port words only mean something for TCP and UDP. */
        if(key[0]!=0x80 || key[1]!=protocol || memcmp(key+8,ip+8,32))return 0;
        if(protocol!=6 && protocol!=17)goto accepted;
        transport=frame+l4;
        if(memcmp(key+4,transport,4))return 0;
        if(protocol==6) {
            if(ip_length<60 || transport[12]>>4<5 || (size_t)(transport[12]>>4)*4>ip_length-40)return 0;
        } else if(be16(transport+4)!=ip_length-40)return 0;
        goto accepted;
    }
    if(ether_type!=0x0800 || packet_type!=1 || (key && key[0]!=0x40))return 0;
    ip=frame+l3;
    protocol=ip[9];
    /* Every IPv4 protocol is data once the envelope, scope and lengths agree:
     * GRE, ESP, IGMP, OSPF, SCTP and the rest belong to the kernel's policy,
     * not to this decoder. Options are allowed; the header length must agree
     * with the chip's metadata. A padded minimum frame is 60 bytes untagged,
     * 64 with its VLAN tag. */
    if(ip[0]>>4!=4 || (ip[0]&15)<5)return 0;
    ip_length=be16(ip+2);l4=l3+(ip[0]&15)*4;
    if(ip_length<(unsigned)((ip[0]&15)*4+4) || ip_length>length-l3 ||
       (ip_length!=length-l3 && length!=(l3==18?64:60)) || ((be16(info+6)>>7)&0xff)!=(unsigned)((ip[0]&15)*4))return 0;
    if(protocol!=6 && protocol!=17 && protocol!=1) {
        /* The chip keys any IPv4 protocol it cannot match, with zero port words
         * (GRE tunnels into the WAN, PA-5220 2026-10-20): the key must name this
         * packet's protocol and addresses. Session punts are TCP by construction. */
        if(cmh[3]==2 || (!key && cmh[3]!=4 && cmh[3]!=5))return 0;
        if(key && (key[0]!=0x40 || key[1]!=ip[9] || memcmp(key+8,ip+12,8)))return 0;
        goto accepted;
    }
    /* Reassembly and policy remain the software dataplane's responsibility:
     * a fragment is data under any message (the chip keys first fragments and
     * may report them under other exception codes); the fragment code itself
     * must name a fragment. Transport headers exist only in first fragments. */
    unsigned fragment=be16(ip+6)&0x3fff,first=!(be16(ip+6)&0x1fff);
    if(cmh[3]==4 && cmh[23]==30 && !fragment)return 0;
    if(cmh[3]==5 && cmh[23]==2 && ip[8]!=1)return 0;
    transport=frame+l4;
    /* A key names this packet's protocol and addresses; its port words are
     * ports only for TCP and UDP (the chip keys ICMP too, with other content). */
    if(key && (key[0]!=0x40 || key[1]!=ip[9] || memcmp(key+8,ip+12,8) ||
               (protocol!=1 && first && memcmp(key+4,transport,4))))return 0;
    if(cmh[3]==2 && (protocol!=6 || fragment))return 0;
    if(protocol==1) {
        /* ICMP arrives as an exception of any code, or keyed like any other
         * protocol the chip cannot match. */
        if((!key && cmh[3]!=4 && cmh[3]!=5) || (first && ip_length<28))return 0;
    } else if(!fragment && ip[9]==6) {
        if(ip_length<40 || transport[12]>>4<5 || (size_t)(transport[12]>>4)*4>ip_length-20)return 0;
    } else if(!fragment && (ip_length<28 || be16(transport+4)!=ip_length-20))return 0;
accepted:
    *out=(struct ffn_fe100_punt){.frame=frame,.length=length,.flow_id=be32(cmh+16),
        .front=port,.in_lif=lif,.zone=zone,.message=cmh[3],.code=cmh[23]};
    return 1;
}
