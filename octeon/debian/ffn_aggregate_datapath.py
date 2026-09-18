#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Software aggregate gates and stable layer-2/3 flow selection.

The same gate map used for readback is consulted for every packet. Gates are
closed initially and on owner failure; there is no alternate BCM bridging path.
"""
import copy
import struct
import zlib


def flow_key(frame):
    if len(frame)<14:raise ValueError('Truncated Ethernet frame')
    offset=14;kind=frame[12:14];vlan=b''
    for _ in range(2):
        if kind not in (b'\x81\x00',b'\x88\xa8'):break
        if len(frame)<offset+4:raise ValueError('Truncated VLAN frame')
        vlan+=frame[offset:offset+2];kind=frame[offset+2:offset+4];offset+=4
    # Do not hash transport ports: all fragments of a flow use the same member.
    if kind==b'\x08\x00' and len(frame)>=offset+20 and frame[offset]>>4==4:
        return vlan+kind+frame[offset+12:offset+20]+frame[offset+9:offset+10]
    if kind==b'\x86\xdd' and len(frame)>=offset+40 and frame[offset]>>4==6:
        return vlan+kind+frame[offset+8:offset+40]
    return vlan+frame[:12]+kind


class Gates:
    def __init__(self,ports):
        self.state={p:dict(collect=False,distribute=False) for p in ports}
        self.rx={p:0 for p in ports};self.tx={p:0 for p in ports};self.dropped=0

    def apply(self,value):
        if set(value)!=set(self.state) or any(set(v)!={'collect','distribute'} or any(type(x) is not bool for x in v.values()) for v in value.values()):
            raise ValueError('Invalid member gate mapping')
        self.state=copy.deepcopy(value)
        return copy.deepcopy(self.state)

    def receive(self,port,frame,deliver):
        if not self.state.get(port,{}).get('collect'):
            self.dropped+=1;return False
        deliver(port,frame);self.rx[port]+=1;return True

    def transmit(self,frame,send):
        ports=sorted(p for p,v in self.state.items() if v['distribute'])
        if not ports:self.dropped+=1;return None
        key=flow_key(frame)
        # Rendezvous hashing only remaps flows whose selected member disappears.
        port=max(ports,key=lambda p:zlib.crc32(key+struct.pack('!H',p)))
        send(port,frame);self.tx[port]+=1;return port
