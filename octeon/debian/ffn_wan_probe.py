#!/usr/bin/env python3
"""Bounded DHCP DISCOVER/OFFER wire qualification. Does not acquire a lease."""
import argparse
from contextlib import contextmanager
from collections import Counter
import fcntl
import ipaddress
import json
from pathlib import Path
import secrets
import select
import socket
import struct
import subprocess
import time
from ffn_dp_packet_transport import encode,decode_otmh_ssp,validate_trunk

FRONT={1:28}
COOKIE=b'\x63\x82\x53\x63'
FABRIC_LOCK=Path('/run/ffn-fabric.lock')
PORT_LOCK=Path('/run/ffn-aggregate-port-1.lock')


@contextmanager
def ownership():
    # Aggregate owners share the trunk but have disjoint ingress/egress ports.
    # The legacy whole-fabric owner and a WAN attachment still exclude a probe.
    with FABRIC_LOCK.open('a') as fabric, PORT_LOCK.open('a') as port:
        fcntl.flock(fabric,fcntl.LOCK_SH|fcntl.LOCK_NB)
        fcntl.flock(port,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield


def mac_address():
    # A modem may admit only one learned CPE MAC. Qualification must use the
    # same identity as the subsequent attachment, never a probe-only MAC.
    result=subprocess.run(['ip','-n','ffn-data','-j','link','show','dev','p1'],
                          check=True,capture_output=True,text=True,timeout=5)
    rows=json.loads(result.stdout)
    if len(rows)!=1 or rows[0].get('ifname')!='p1' or rows[0].get('link_type')!='ether':
        raise RuntimeError('WAN interface identity unavailable')
    mac=bytes.fromhex(rows[0]['address'].replace(':',''))
    if len(mac)!=6 or mac==bytes(6) or mac[0]&1:raise RuntimeError('Invalid WAN interface MAC')
    return mac


def checksum(value):
    if len(value)%2:value+=b'\0'
    total=sum(struct.unpack('!%dH'%(len(value)//2),value))
    while total>>16:total=(total&65535)+(total>>16)
    return (~total)&65535


def discover(mac,xid):
    boot=struct.pack('!BBBBIHH',1,1,6,0,xid,0,0x8000)+bytes(16)+mac+bytes(10)+bytes(64+128)+COOKIE
    data=boot+b'\x35\x01\x01\x3d\x07\x01'+mac+b'\x37\x04\x01\x03\x06\x33\xff'
    data+=bytes(max(0,300-len(data)))
    udp=struct.pack('!HHHH',68,67,len(data)+8,0)+data
    ip=struct.pack('!BBHHHBBH4s4s',0x45,0,20+len(udp),xid&65535,0,64,17,0,bytes(4),b'\xff'*4)
    ip=ip[:10]+struct.pack('!H',checksum(ip))+ip[12:]
    return b'\xff'*6+mac+b'\x08\x00'+ip+udp


def offer(frame,mac,xid):
    if len(frame)<14+20+8+240 or frame[:6] not in (mac,b'\xff'*6) or frame[12:14]!=b'\x08\x00':return None
    packet=frame[14:];ihl=(packet[0]&15)*4;total=int.from_bytes(packet[2:4],'big')
    if packet[0]>>4!=4 or ihl<20 or total>len(packet) or total<ihl+248 or packet[9]!=17 or checksum(packet[:ihl]):return None
    if int.from_bytes(packet[6:8],'big')&0x3fff:return None
    udp=packet[ihl:total];sport,dport,length,check=struct.unpack_from('!HHHH',udp)
    if (sport,dport)!=(67,68) or length!=len(udp):return None
    if check and checksum(packet[12:20]+b'\0\x11'+struct.pack('!H',length)+udp):return None
    data=udp[8:]
    if data[:3]!=b'\x02\x01\x06' or struct.unpack_from('!I',data,4)[0]!=xid or data[28:34]!=mac or data[236:240]!=COOKIE:return None
    options={};pos=240;ended=False
    while pos<len(data):
        tag=data[pos];pos+=1
        if tag==255:ended=True;break
        if tag==0:continue
        if pos>=len(data) or pos+1+data[pos]>len(data):return None
        length=data[pos];pos+=1
        if tag in options:return None
        options[tag]=data[pos:pos+length];pos+=length
    if not ended or options.get(53)!=b'\x02' or len(options.get(54,b''))!=4:return None
    address=ipaddress.IPv4Address(data[16:20]);server=ipaddress.IPv4Address(options[54])
    if any(ip.is_unspecified or ip.is_multicast or ip.is_loopback or int(ip)==0xffffffff for ip in (address,server)):return None
    return {'offered_address':str(address),'server_identifier':str(server)}


def probe(seconds=12):
    if type(seconds) is not int or not 3<=seconds<=20:raise ValueError('probe duration must be 3..20 seconds')
    validate_trunk('ffnpkt0')
    mac=mac_address();xid=secrets.randbits(32);sent=0;matched=None
    counters=Counter();sources=Counter();protocols=Counter()
    with ownership(), socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3)) as conn:
        conn.bind(('ffnpkt0',0));conn.setblocking(False)
        begin=time.monotonic();next_send=begin
        while time.monotonic()<begin+seconds:
            if time.monotonic()>=next_send and sent<3:
                packet=encode(1,discover(mac,xid),FRONT)
                if conn.send(packet)!=len(packet):raise RuntimeError('short DHCP probe write')
                sent+=1;next_send+=3
            if not select.select([conn],[],[],.1)[0]:continue
            raw,peer=conn.recvfrom(65536)
            if peer[2]==socket.PACKET_OUTGOING:continue
            counters['trunk_rx']+=1
            if len(raw)>=4 and raw[:2]==b'\0\x18':
                source=int.from_bytes(raw[2:4],'big')
                sources[str(source) if source<=36 else 'other']+=1
            item=decode_otmh_ssp(raw,{1},FRONT)
            if item:
                counters['wan_rx']+=1
                protocol=item[1][12:14].hex()
                protocols[protocol if protocol in ('0800','0806','86dd','8100','88a8') else 'other']+=1
                matched=offer(item[1],mac,xid)
            else:counters['envelope_rejected']+=1
            if matched:break
    return {'port':1,'bcm_port':28,'mac':mac.hex(':'),'xid':xid,'discover_sent':sent,
            'dhcp_offer_verified':bool(matched),'lease_acquired':False,'offer':matched,
            'counters':dict(counters),'return_sources':dict(sources),'wan_ethertypes':dict(protocols),
            'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}


def status():
    validate_trunk('ffnpkt0')
    available=True
    try:
        with ownership():pass
    except BlockingIOError:available=False
    return {'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'fabric_available':available,'port':1,'bcm_port':28}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--seconds',type=int,default=12)
    parser.add_argument('--status',action='store_true');args=parser.parse_args()
    print(json.dumps(status() if args.status else probe(args.seconds)))
