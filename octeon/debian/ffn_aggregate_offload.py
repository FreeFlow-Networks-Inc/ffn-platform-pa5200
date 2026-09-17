# SPDX-License-Identifier: GPL-2.0-or-later
"""Leased CP acknowledgement for Jericho egress LAG transmission."""
import math
import struct
from ffn_dp_packet_transport import encode,FRONT


class Offload:
    def __init__(self,tid,ports):
        if type(tid) is not int or not 1<=tid<=12:raise ValueError('Invalid trunk ID')
        self.tid=tid;self.ports=set(ports);self.members=[];self.deadline=0;self.transmitted=0
        # CP only ever programs the complete sorted set or an empty trunk.
        # No member index is reused during a membership transition.
        self.aliases={0x8000|(index<<8)|tid:FRONT[port] for index,port in enumerate(sorted(ports))}

    def ingress(self,raw):
        if len(raw)<18 or raw[:2]!=b'\0\x18':return raw
        port=self.aliases.get(struct.unpack_from('!H',raw,2)[0])
        return raw[:2]+struct.pack('!H',port)+raw[4:] if port is not None else raw

    def acknowledge(self,value,now,age):
        self.members=[];self.deadline=0
        if value is None:return
        ports=value.get('members')
        if (value.get('tid')!=self.tid or value.get('verified') is not True or value.get('exists') is not True
            or value.get('psc')!=9 or value.get('ingress_metadata')!='physical-or-fixed-spa'
            or not isinstance(ports,list) or any(type(p) is not int or p not in self.ports for p in ports)
            or ports not in ([],sorted(self.ports)) or not math.isfinite(age) or not 0<=age<5):
            raise ValueError('Invalid hardware offload acknowledgement')
        self.members=ports;self.deadline=now+6-age

    def ready(self,gates,now):
        return bool(self.members) and now<self.deadline and self.members==sorted(p for p,v in gates.state.items() if v['distribute'])

    def transmit(self,frame,gates,now,send):
        if not self.ready(gates,now):return False
        # Jericho system-port destination with logical-system-port LAG bit 15.
        # Keep ITMH TC/DP and the commissioned RAW_DSA layout unchanged.
        send(encode(self.tid,frame,{self.tid:0x8000|self.tid}))
        self.transmitted+=1
        return True
