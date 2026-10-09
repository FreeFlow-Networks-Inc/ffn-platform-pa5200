"""Coordinate native FE100 counters with an existing native UDP CT lease.

This is control orchestration, not a packet path or admission authority.
Caller owns serialization, connection identity, policy/path generation checks,
the independent withdrawal watchdog, and hardware entry installation. It must
bind the lease and register both fresh flow IDs BEFORE installing either entry.
"""
from ffn_fe100_flowstats import flow_id_of
from ffn_fe100_sessions import validate_entry4,output_key4
import socket
import struct


def flow_tuple(key):
    _,_,_,sport,dport,src,dst=struct.unpack('!BBHHH4s4s',key[:16])
    return socket.inet_ntoa(src),socket.inet_ntoa(dst),sport,dport


def reverse(value):return value[1],value[0],value[3],value[2]


class AccountedSession:
    def __init__(self, stream, entries, lease, withdraw):
        if len(entries)!=2:raise ValueError('both directions required')
        self.entries=tuple(validate_entry4(e) for e in entries)
        if any(e[1]!=17 for e in self.entries):raise ValueError('only UDP accounting is qualified')
        if len({flow_id_of(e) for e in self.entries})!=2:raise ValueError('distinct directional flow IDs required')
        if (lease.protocol!=17 or tuple(flow_tuple(e) for e in self.entries)!=(lease.original,lease.reply) or
                tuple(flow_tuple(output_key4(e)) for e in self.entries)!=(reverse(lease.reply),reverse(lease.original))):
            raise ValueError('hardware NAT directions do not match the bound conntrack object')
        if not callable(withdraw):raise ValueError('acknowledged withdrawal callback required')
        stream.check_receiver()
        snapshots=[stream.accounting(e) for e in self.entries]
        if any(s['report_at'] is not None for s in snapshots):raise ValueError('bind before hardware reports')
        self.stream,self.epoch,self.lease,self.withdraw=stream,stream.epoch,lease,withdraw
        self.previous=snapshots
        self.sequence=0
        self.failure=None
        self.closed=False

    def fence(self, reason):
        """Keep the kernel reference until BOTH hardware directions are gone."""
        self.failure=str(reason)
        if self.closed:return
        if self.withdraw() is not True:raise RuntimeError('hardware withdrawal not acknowledged')
        self.lease.close()
        self.closed=True
        for entry in self.entries:self.stream.forget(flow_id_of(entry))

    def sync(self):
        if self.failure is not None or self.closed:raise RuntimeError('accounting session fenced')
        try:
            if self.stream.epoch!=self.epoch:raise RuntimeError('hardware epoch changed')
            self.stream.check_receiver()
            snapshots=[self.stream.accounting(e) for e in self.entries]
            now=self.stream.now()
            packets=[];octets=[];active=0
            for direction,(current,previous) in enumerate(zip(snapshots,self.previous)):
                delta={k:current[k]-previous[k] for k in ('packets','octets','activity')}
                if any(v<0 for v in delta.values()):raise RuntimeError('counter totals regressed')
                if delta['packets'] or delta['octets']:
                    age=now-current['report_at']
                    if age<0 or age>self.stream.freshness:raise RuntimeError('hardware accounting arrived too late')
                if delta['activity']:
                    if self.stream(self.entries[direction]) is None:raise RuntimeError('flow activity is stale')
                    active|=1<<direction
                packets.append(delta['packets']);octets.append(delta['octets'])
            if not any(packets) and not any(octets):
                # A deleted/expired kernel session must be withdrawn even if
                # hardware sends no further reports. This does not refresh it.
                self.lease.check()
                return False
            # The native kernel endpoint rejects impossible deltas, stale
            # objects and sequence replay. Never retry an ambiguous failure.
            self.lease.update(self.sequence+1,tuple(packets),tuple(octets),active)
            self.sequence+=1
            self.previous=snapshots
            return True
        except BaseException as error:
            self.fence(error)
            raise
