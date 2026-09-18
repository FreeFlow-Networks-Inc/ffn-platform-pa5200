# SPDX-License-Identifier: GPL-2.0-or-later
"""Transport-independent LACP negotiation with acknowledged member gates.

The owner supplies monotonic time, leased carrier/speed and received Ethernet
frames. The driver must synchronously gate collection/distribution and return
exact readback. Nothing here opens sockets, changes BCM state or infers carrier.
"""
from collections import Counter
import math
import struct
from ffn_lacp_packets import decode,DESTINATION

ACTIVITY,TIMEOUT,AGGREGATION,SYNC,COLLECTING,DISTRIBUTING,DEFAULTED,EXPIRED=(1<<n for n in range(8))
IDENTITY=('system_priority','system','key','port_priority','port')
ZERO=dict(system_priority=0,system='00:00:00:00:00:00',key=0,port_priority=0,port=0,state=0)


def mac_bytes(value):
    if not isinstance(value,str) or len(value)!=17:raise ValueError('MAC must have six colon-separated octets')
    try:raw=bytes(int(v,16) for v in value.split(':'))
    except (ValueError,OverflowError):raise ValueError('Invalid MAC')
    if len(raw)!=6 or any(len(v)!=2 for v in value.split(':')):raise ValueError('Invalid MAC')
    return raw


def identity(value):return tuple(value[k] for k in IDENTITY)


def encode(actor,partner,source):
    raw=mac_bytes(source)
    if raw[0]&1 or raw==bytes(6):raise ValueError('Unicast source MAC required')
    def tlv(kind,p):
        return struct.pack('!BBH6sHHHB3x',kind,20,p['system_priority'],mac_bytes(p['system']),
                           p['key'],p['port_priority'],p['port'],p['state'])
    return DESTINATION+raw+b'\x88\x09\x01\x01'+tlv(1,actor)+tlv(2,partner)+b'\x03\x10'+bytes(66)


class Engine:
    def __init__(self,system,key,members,driver,*,activity='active',rate='fast',system_priority=32768,min_links=1,collecting=True):
        raw=mac_bytes(system)
        if raw==bytes(6) or raw[0]&1:raise ValueError('Unicast system MAC required')
        if type(key) is not int or not 1<=key<=65535:raise ValueError('Invalid actor key')
        if type(system_priority) is not int or not 1<=system_priority<=65535:raise ValueError('Invalid system priority')
        if (not isinstance(members,dict) or not 1<=len(members)<=8 or
            any(type(p) is not int or not 1<=p<=65535 for p in members)):raise ValueError('Invalid members')
        if type(min_links) is not int or not 1<=min_links<=len(members):raise ValueError('Invalid minimum links')
        if activity not in ('active','passive') or rate not in ('fast','slow'):raise ValueError('Invalid LACP mode')
        if type(collecting) is not bool:raise ValueError('Invalid collection permission')
        self.collecting=collecting
        self.system=system.lower();self.key=key;self.priority=system_priority
        self.active=activity=='active';self.fast=rate=='fast';self.min_links=min_links;self.driver=driver
        self.members={}
        for port,source in members.items():
            raw=mac_bytes(source)
            if raw[0]&1 or raw==bytes(6):raise ValueError('Unicast member MAC required')
            self.members[port]=dict(source=source,link=False,speed=0,lease=0,peer=None,echo=None,
                rx='disabled',deadline=0,selected=False,selected_since=0,matched=False,
                sync=False,last_tx=float('-inf'),last_pdu=None,dirty=True,tx=0,rx_count=0,invalid=0)
        self.bucket=None;self.now=None;self.fault=None;self.applied=None
        # Initialization must close every gate before any LACPDU is advertised.
        self._apply(self.closed())

    def closed(self):return {p:{'collect':False,'distribute':False} for p in self.members}

    def _time(self,now):
        if not isinstance(now,(float,int)) or not math.isfinite(now) or now<0:raise ValueError('Invalid monotonic time')
        if self.now is not None and now<self.now:raise ValueError('Monotonic clock moved backward')
        self.now=now

    def _apply(self,desired):
        if self.fault:return
        if desired==self.applied:return
        try:
            result=self.driver.apply({p:dict(v) for p,v in desired.items()})
            if result!=desired:raise RuntimeError('Member gate readback mismatch')
            self.applied={p:dict(v) for p,v in result.items()}
        except Exception as error:
            self.fault=str(error) or type(error).__name__;self.applied=None
            # Best-effort withdrawal. A failed withdrawal remains uncertain,
            # requiring owner recovery rather than silently retrying enables.
            try:
                off=self.closed()
                if self.driver.apply(off)==off:self.applied=off
            except Exception:pass

    def link(self,port,up,speed,now,lease_seconds=3):
        self._time(now);m=self.members[port]
        if type(up) is not bool or type(speed) is not int or speed<0 or up and speed==0:raise ValueError('Invalid carrier/speed')
        if not isinstance(lease_seconds,(int,float)) or not 0<lease_seconds<=30:raise ValueError('Carrier lease must be 0..30 seconds')
        if (m['link'],m['speed'])!=(up,speed):
            m.update(peer=None,echo=None,matched=False,selected=False,sync=False,
                     rx='expired' if up else 'disabled',deadline=now+3,dirty=True)
        m.update(link=up,speed=speed,lease=now+lease_seconds)
        self.tick(now)

    def actor(self,port):
        m=self.members[port];state=AGGREGATION|(ACTIVITY if self.active else 0)|(TIMEOUT if self.fast else 0)
        if m['rx']=='defaulted':state|=DEFAULTED
        if m['rx']=='expired':state|=EXPIRED
        if m['sync'] and not self.fault:state|=SYNC
        applied=(self.applied or {}).get(port,{})
        if applied.get('collect') and not self.fault:state|=COLLECTING
        if applied.get('distribute') and not self.fault:state|=DISTRIBUTING
        return dict(system_priority=self.priority,system=self.system,key=self.key,port_priority=32768,port=port,state=state)

    def receive(self,port,frame,now):
        self._time(now);self.tick(now);m=self.members[port]
        if not m['link'] or now>=m['lease'] or self.fault:return False
        try:pdu=decode(frame)
        except ValueError:m['invalid']+=1;return False
        peer=pdu['actor'];echo=pdu['partner']
        if peer['system']==self.system:
            m['invalid']+=1;return False
        if m['peer'] is None or identity(m['peer'])!=identity(peer) or bool(m['peer']['state']&AGGREGATION)!=bool(peer['state']&AGGREGATION):
            m.update(selected=False,sync=False,dirty=True)
        actor=self.actor(port)
        matched=(identity(echo)==identity(actor) and bool(echo['state']&AGGREGATION)
                 and bool(peer['state']&ACTIVITY or self.active and echo['state']&ACTIVITY))
        # Request an update only if our current advertisement or recorded peer
        # differs, not for every identical periodic receive.
        if (m['peer'] is None or identity(m['peer'])!=identity(peer) or m['peer']['state']!=peer['state']
                or identity(echo)!=identity(actor) or (echo['state']^actor['state'])&(ACTIVITY|TIMEOUT|AGGREGATION|SYNC|COLLECTING|DISTRIBUTING)):
            m['dirty']=True
        m.update(peer=peer,echo=echo,matched=matched,rx='current',deadline=now+(3 if self.fast else 90),rx_count=m['rx_count']+1)
        self.tick(now);return True

    def tick(self,now):
        self._time(now)
        for m in self.members.values():
            if not m['link'] or now>=m['lease']:
                m.update(link=False,rx='disabled',peer=None,echo=None,matched=False)
            elif m['rx']=='current' and now>=m['deadline']:
                m.update(rx='expired',matched=False,deadline=m['deadline']+3,dirty=True)
                if m['peer']:m['peer']=dict(m['peer'],state=(m['peer']['state']&~SYNC)|TIMEOUT)
            if m['rx']=='expired' and now>=m['deadline']:
                m.update(rx='defaulted',peer=None,echo=None,matched=False,dirty=True)
        buckets={}
        duplicate=Counter((m['peer']['system'],m['peer']['port']) for m in self.members.values() if m['rx']=='current' and m['peer'])
        for port,m in self.members.items():
            peer=m['peer']
            if m['rx']!='current' or not peer or not peer['state']&AGGREGATION or duplicate[peer['system'],peer['port']]!=1:continue
            group=(peer['system_priority'],peer['system'],peer['key'],m['speed'])
            buckets.setdefault(group,[]).append(port)
        if self.bucket not in buckets:
            self.bucket=min(buckets,key=lambda g:(-len(buckets[g])*g[3],g)) if buckets else None
        selected=set(buckets.get(self.bucket,[]));desired=self.closed();eligible=[]
        for port,m in self.members.items():
            was=m['sync']
            if port in selected and not m['selected']:m['selected_since']=now
            m['selected']=port in selected
            m['sync']=m['selected'] and now-m['selected_since']>=2 and not self.fault
            if was!=m['sync']:m['dirty']=True
            peer=m['peer']
            if self.collecting and m['sync'] and m['matched'] and peer['state']&SYNC:
                desired[port]['collect']=True
                if peer['state']&COLLECTING:eligible.append(port)
        if len(eligible)>=self.min_links:
            for port in eligible:desired[port]['distribute']=True
        self._apply(desired)

    def transmissions(self,now):
        self.tick(now);result=[]
        for port,m in self.members.items():
            peer=m['peer'] or ZERO
            if not m['link'] or self.fault or not (self.active or peer['state']&ACTIVITY):continue
            interval=1 if not m['peer'] or peer['state']&TIMEOUT else 30
            frame=encode(self.actor(port),peer,m['source'])
            changed=frame!=m['last_pdu']
            if now-m['last_tx']>=1/3 and (m['dirty'] or changed or now-m['last_tx']>=interval):
                # Limit attempts as well as successful sends to <=3 PDUs/s.
                m.update(last_tx=now,dirty=False,last_pdu=frame,tx=m['tx']+1)
                result.append((port,frame))
        return result

    def stop(self,now):
        self._time(now)
        for m in self.members.values():m.update(link=False,rx='disabled',peer=None,echo=None,sync=False,selected=False)
        if self.fault:
            # Explicit owner shutdown retries withdrawal even after an earlier
            # driver failure. The fault stays latched; this cannot enable gates.
            self.applied=None
            try:
                off=self.closed()
                if self.driver.apply(off)==off:self.applied=off
            except Exception:pass
        else:self._apply(self.closed())

    def abort(self,reason,now):
        """Latch an owner/transport failure and attempt verified withdrawal."""
        self._time(now)
        self.fault=self.fault or str(reason) or 'LACP owner failed'
        self.stop(now)

    def status(self,now):
        self.tick(now)
        return dict(fault=self.fault,gates_verified=self.applied is not None,
            distributing=[p for p,v in (self.applied or {}).items() if v['distribute'] and not self.fault],
            members={p:dict(rx=m['rx'],selected=m['selected'],matched=m['matched'],
                actor=self.actor(p),peer=m['peer'],tx_attempts=m['tx'],received=m['rx_count'],invalid=m['invalid'],
                gates=(self.applied or {}).get(p)) for p,m in self.members.items()})
