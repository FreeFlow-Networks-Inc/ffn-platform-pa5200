#!/usr/bin/env python3
"""Bounded DP wire qualification of Linux VIF bridging/routing on the 5--13 DAC.

Requires fv4001=(13,3901), fv4002=(5,3902). Distinct return selectors prevent
recirculation. No configuration writes: the MP harness owns setup and cleanup.
"""
import argparse
import ipaddress
import json
import select
import socket
import struct
import subprocess
import time
import uuid
from ffn_dp_packet_transport import encode, decode_otmh_ssp, validate_trunk


def checksum(data):
    if len(data) % 2: data += b'\0'
    total = sum(struct.unpack('!%dH' % (len(data)//2), data))
    while total >> 16: total = (total & 65535) + (total >> 16)
    return (~total) & 65535


def tagged(frame, vlan):
    return frame[:12] + b'\x81\x00' + struct.pack('!H', vlan) + frame[12:]


def neighbor_reply(frame, gateway, endpoint_mac):
    """Emulate only the reserved test next hop, never a general network peer."""
    address=ipaddress.ip_address(gateway)
    if address.version==4:
        if (len(frame)<42 or frame[12:22]!=bytes.fromhex('08060001080006040001')
                or frame[38:42]!=address.packed):return None
        arp=bytes.fromhex('0001080006040002')+endpoint_mac+address.packed+frame[22:28]+frame[28:32]
        return frame[6:12]+endpoint_mac+b'\x08\x06'+arp
    if (len(frame)<78 or frame[12:14]!=b'\x86\xdd' or frame[20:22]!=bytes([58,255])
            or frame[54:56]!=bytes([135,0]) or frame[62:78]!=address.packed):return None
    source=address.packed;destination=frame[22:38]
    if destination==bytes(16):return None # do not answer duplicate-address probes
    icmp=bytes([136,0,0,0])+struct.pack('!I',0x60000000)+source+bytes([2,1])+endpoint_mac
    pseudo=source+destination+struct.pack('!I3xB',len(icmp),58)
    icmp=icmp[:2]+struct.pack('!H',checksum(pseudo+icmp))+icmp[4:]
    header=struct.pack('!IHBB',6<<28,len(icmp),58,255)+source+destination
    return frame[6:12]+endpoint_mac+b'\x86\xdd'+header+icmp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('l2','ipv4','ipv6'))
    parser.add_argument('--expect', type=int, choices=(0,4), default=4)
    parser.add_argument('--resolve-neighbors', action='store_true')
    args = parser.parse_args()
    validate_trunk('ffnpkt0')
    links = {p['ifname']:p for p in json.loads(subprocess.check_output(['ip','-n','ffn-data','-j','link']))}
    mac = lambda name: bytes.fromhex(links[name]['address'].replace(':',''))
    report = {'mode':args.mode, 'directions':[]}
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3)) as wire:
        wire.bind(('ffnpkt0',0)); wire.setblocking(False)
        for reverse in (False,True):
            src,dst = (('fv4002','fv4001') if reverse else ('fv4001','fv4002'))
            input_port,returned_port = ((13,5) if reverse else (5,13))
            ivlan,ovlan = ((3902,3901) if reverse else (3901,3902))
            src_sub,dst_sub = ((202,201) if reverse else (201,202))
            source_mac = bytes.fromhex('02ff00000002' if reverse else '02ff00000001')
            dest_mac = bytes.fromhex('02ff00000001' if reverse else '02ff00000002')
            expected=[];inputs=[];nonce=uuid.uuid4().bytes
            for sequence in range(4):
                payload=nonce+bytes([sequence])+bytes(31)
                if args.mode=='l2':
                    frame=dest_mac+source_mac+b'\x88\xb5'+payload
                    output=frame
                else:
                    ipv6=args.mode=='ipv6'
                    sip=ipaddress.ip_address('2001:db8:%d::2'%src_sub if ipv6 else '198.18.%d.2'%src_sub).packed
                    dip=ipaddress.ip_address('2001:db8:%d::2'%(dst_sub+100) if ipv6 else '198.19.%d.2'%dst_sub).packed
                    udp=struct.pack('!HHHH',45001,45002,8+len(payload),0)+payload
                    if ipv6:
                        pseudo=sip+dip+struct.pack('!I3xB',len(udp),17)
                        udp=udp[:6]+struct.pack('!H',checksum(pseudo+udp) or 65535)+udp[8:]
                        header=struct.pack('!IHBB',6<<28,len(udp),17,64)+sip+dip
                        forwarded=header[:7]+b'\x3f'+header[8:]
                        ether=b'\x86\xdd'
                    else:
                        header=struct.pack('!BBHHHBBH',0x45,0,20+len(udp),sequence,0,64,17,0)+sip+dip
                        header=header[:10]+struct.pack('!H',checksum(header))+header[12:]
                        forwarded=header[:8]+b'\x3f'+header[9:10]+b'\0\0'+header[12:]
                        forwarded=forwarded[:10]+struct.pack('!H',checksum(forwarded))+forwarded[12:]
                        ether=b'\x08\x00'
                    frame=mac(src)+source_mac+ether+header+udp
                    output=dest_mac+mac(dst)+ether+forwarded+udp
                inputs.append(tagged(frame,ivlan));expected.append(tagged(output,ovlan))
            seen=set();ingress=set();bad=[];replies=0
            for frame in inputs:
                wire.send(encode(input_port,frame));time.sleep(.05)
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                if not select.select([wire],[],[],.1)[0]:continue
                raw,addr=wire.recvfrom(65536)
                if addr[2]==socket.PACKET_OUTGOING:continue
                decoded=decode_otmh_ssp(raw,{5,13})
                if not decoded:continue
                port,frame=decoded
                if args.resolve_neighbors and args.mode!='l2' and port==returned_port and frame[12:16]==b'\x81\x00'+struct.pack('!H',ovlan):
                    gateway='2001:db8:%d::2'%dst_sub if args.mode=='ipv6' else '198.18.%d.2'%dst_sub
                    reply=neighbor_reply(frame[:12]+frame[16:],gateway,dest_mac)
                    if reply:
                        wire.send(encode(returned_port,tagged(reply,ovlan)));replies+=1
                if nonce not in frame:continue
                if port==returned_port and frame in inputs:ingress.add(inputs.index(frame))
                elif port==returned_port and frame in expected:seen.add(expected.index(frame))
                else:bad.append({'port':port,'frame':frame.hex()})
            result={'ingress_vif':src,'egress_vif':dst,'sent':4,'physical_ingress':len(ingress),
                    'exact_forwarded':len(seen),'expected':args.expect,'unexpected':bad[:8],
                    'neighbor_replies':replies}
            report['directions'].append(result)
    report['passed']=all(r['physical_ingress']==4 and r['exact_forwarded']==args.expect and not r['unexpected'] for r in report['directions'])
    if args.resolve_neighbors and args.mode!='l2':
        report['passed']=report['passed'] and all(r['neighbor_replies']>0 for r in report['directions'])
    print(json.dumps(report,indent=2),flush=True)
    if not report['passed']:raise SystemExit(1)


if __name__=='__main__':main()
