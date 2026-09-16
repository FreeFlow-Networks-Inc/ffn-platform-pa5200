# SPDX-License-Identifier: GPL-2.0-or-later
"""Strict LACP wire decoding and expiring, per-port partner observations.

Wire layout reference: Linux include/net/bond_3ad.h. Observing a peer is not
permission to collect/distribute traffic or proof of local negotiation.
"""
import struct

DESTINATION=bytes.fromhex('0180c2000002')
FLAGS=('activity','short_timeout','aggregation','synchronization','collecting',
       'distributing','defaulted','expired')


def mac(raw):return ':'.join('%02x'%b for b in raw)


def decode(frame):
    if not isinstance(frame,bytes) or len(frame)<124:
        raise ValueError('truncated LACP frame')
    if frame[:6]!=DESTINATION or frame[12:16]!=b'\x88\x09\x01\x01':
        raise ValueError('not an untagged LACP version 1 frame')
    if frame[6]&1 or frame[6:12]==bytes(6):raise ValueError('invalid source MAC')
    def info(offset,kind):
        if frame[offset:offset+2]!=bytes((kind,20)):raise ValueError('invalid LACP information TLV')
        priority,system,key,port_priority,port,state=struct.unpack_from('!H6sHHHB',frame,offset+2)
        return dict(system_priority=priority,system=mac(system),key=key,
                    port_priority=port_priority,port=port,state=state,
                    flags={name:bool(state & (1<<i)) for i,name in enumerate(FLAGS)})
    actor=info(16,1);partner=info(36,2)
    if frame[56:58]!=b'\x03\x10' or frame[72:74]!=b'\0\0':raise ValueError('invalid collector or terminator TLV')
    if actor['system']=='00:00:00:00:00:00' or int(actor['system'][:2],16)&1 or actor['port']==0:
        raise ValueError('invalid actor identity')
    # Reserved octets and padding are ignored as receivers must tolerate them.
    return dict(source=mac(frame[6:12]),actor=actor,partner=partner,
                collector_max_delay=struct.unpack_from('!H',frame,58)[0])


class Observations:
    def __init__(self):self.ports={};self.invalid=0;self.received=0

    def receive(self,port,frame,now):
        if type(port) is not int or not 1<=port<=24:raise ValueError('invalid faceplate port')
        try:pdu=decode(frame)
        except ValueError:self.invalid+=1;return False
        prior=self.ports.get(port,{})
        self.received+=1
        self.ports[port]=dict(pdu,received=prior.get('received',0)+1,last_monotonic=now)
        return True

    def snapshot(self,now):
        rows=[]
        for port,pdu in sorted(self.ports.items()):
            age=now-pdu['last_monotonic']
            # Partner TLV describes our advertised timeout preference. Actor
            # Timeout describes what the remote requests from our transmitter.
            timeout=3 if pdu['partner']['flags']['short_timeout'] else 90
            expired=not 0<=age<timeout
            rows.append(dict(port=port,received=pdu['received'],age_seconds=max(0,age),
                             expired=expired,timeout_seconds=timeout,
                             actor=pdu['actor'],partner=pdu['partner'],
                             negotiated=False,forwarding_verified=False))
        return dict(ports=rows,received=self.received,invalid=self.invalid,observation_only=True)
