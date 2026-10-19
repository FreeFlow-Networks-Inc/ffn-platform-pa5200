"""Bounded DP session stream framing and observation-only CP receiver.

This module cannot program hardware. Its transport must authenticate both
endpoints; nonce/sequence checks supply continuity, not authentication.

Stream operations: `begin` (policy context, optional `l3` topology generation and
optional `limits`), `snapshot` chunks, `synchronized`, then live `changes` batches
(ordered upserts and closes from one conntrack burst), the per-event `upsert` and
`close` forms a legacy producer still emits, and `heartbeat`. A batch costs the
relay one CP round trip, which is what lets the MP keep up with a busy table.
"""
import copy
import ipaddress
import json
import math
import os
from pathlib import Path
import select
import time
import uuid

MAX_FRAME=524288
CAPACITY=8192
# The MP relay's reports (session_relay.py writes them; the session views read them).
RELAY_REPORT=Path('/run/ffn-fe100-session-relay.json')
RELAY_INVENTORY=Path('/run/ffn-fe100-session-inventory.json')
# Entries one `changes` message may carry; a snapshot chunk has the same bound.
BATCH=128
# The MP inventory report holds every compact row; a full table is a few MiB.
INVENTORY_BYTES=32*1024*1024
INVENTORY_FRESH_SECONDS=15


def canonical_uuid(value):
    if not isinstance(value,str) or str(uuid.UUID(value))!=value:raise ValueError('Canonical UUID required')
    return value


def sha(value):
    if not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('SHA256 identity required')


def validate_l3(row,policy):
    """Bind directional next-hop evidence to this observation generation.

    This is not a hardware lease or an assertion that exceptions are wired.
    Legacy peers remain readable, but cannot provide L3-ready observations.
    """
    context=policy.get('l3')
    if context is None:
        if 'l3' in row:raise ValueError('L3 row has no topology generation')
        return
    value=row.get('l3')
    if (not isinstance(value,dict) or value.get('hardware_admission') is not False or
            value.get('snapshot_digest')!=context['snapshot_digest'] or
            type(value.get('available')) is not bool or not isinstance(value.get('blockers'),list) or
            any(not isinstance(v,str) for v in value['blockers'])):
        raise ValueError('Invalid L3 generation or observation')
    if not value['available']:
        if not value['blockers'] or value.get('directions')!=[]:raise ValueError('Invalid blocked L3 observation')
        return
    pairs=(row.get('rule') or {}).get('interface_pairs')
    # The producer names the pair the routes selected; a legacy producer only
    # planned sessions whose rule authorised exactly one pair.
    pair=value.get('pair')
    if pair is None and isinstance(pairs,list) and len(pairs)==1:pair=pairs[0]
    if (not row['software_candidate'] or value['blockers'] or not isinstance(pairs,list) or not pairs or
            not isinstance(pair,list) or len(pair)!=2 or list(pair) not in [list(p) for p in pairs if isinstance(p,list)] or
            not isinstance(value.get('directions'),list) or len(value['directions'])!=2):
        raise ValueError('Invalid directional L3 observation')
    destinations=[row['translated']['destination'],row['original']['source']]
    for direction,name,destination in zip(value['directions'],reversed(pair),destinations):
        binding=policy['bindings'].get(name)
        if (not isinstance(direction,dict) or not isinstance(binding,dict) or
                direction.get('interface')!=name or direction.get('destination')!=destination or
                any(direction.get(k)!=binding.get(k) for k in ('device','index','alias')) or
                type(direction.get('mtu')) is not int or not 576<=direction['mtu']<=9216 or
                direction.get('decrement_ttl') is not True or
                direction.get('exceptions')!=['ttl-expired','mtu-exceeded','ipv4-fragments']):
            raise ValueError('L3 next hop disagrees with acknowledged bindings')
        ipaddress.IPv4Address(direction['next_hop']);ipaddress.IPv4Address(destination)
        vlan=direction.get('vlan')
        if vlan is not None and (type(vlan) is not int or not 1<=vlan<=4094):
            raise ValueError('Invalid L3 VLAN')
        for field in ('source_mac','destination_mac'):
            mac=direction.get(field)
            if not isinstance(mac,str) or len(mac)!=17 or any(len(p)!=2 for p in mac.split(':')):
                raise ValueError('Invalid L3 Ethernet address')
            raw=bytes.fromhex(mac.replace(':',''))
            if len(raw)!=6 or not any(raw) or raw[0]&1:raise ValueError('Invalid L3 unicast address')


def validate_limits(value):
    """The producer's table bounds: the kernel's conntrack maximum and this stream's capacity."""
    if (not isinstance(value,dict) or set(value)!={'conntrack_max','stream_capacity'} or
            any(type(value[k]) is not int or not 0<=value[k]<2**31 for k in value)):
        raise ValueError('Invalid session table limits')


class Lines:
    """Unbuffered, bounded framing; partial lines cannot extend the deadline."""
    def __init__(self,fd):self.fd=fd;self.buffer=bytearray()

    def read(self,timeout=10):
        deadline=time.monotonic()+timeout
        while b'\n' not in self.buffer:
            remaining=deadline-time.monotonic()
            if remaining<=0 or not select.select([self.fd],[],[],remaining)[0]:raise TimeoutError('Session stream timed out')
            data=os.read(self.fd,65536)
            if not data:raise EOFError('Session stream closed')
            self.buffer.extend(data)
            if len(self.buffer.split(b'\n',1)[0])>MAX_FRAME:raise ValueError('Session frame exceeds limit')
        line,_,rest=self.buffer.partition(b'\n');self.buffer=bytearray(rest)
        if len(line)>MAX_FRAME:raise ValueError('Session frame exceeds limit')
        value=json.loads(line)
        if not isinstance(value,dict):raise ValueError('Session frame must be an object')
        return value


def send(fd,value,timeout=10):
    raw=json.dumps(value,separators=(',',':'),allow_nan=False).encode()+b'\n'
    if len(raw)>MAX_FRAME:raise ValueError('Session frame exceeds limit')
    deadline=time.monotonic()+timeout;offset=0
    previous=os.get_blocking(fd);os.set_blocking(fd,False)
    try:
        while offset<len(raw):
            remaining=deadline-time.monotonic()
            if remaining<=0 or not select.select([],[fd],[],remaining)[1]:raise TimeoutError('Session output stalled')
            try:count=os.write(fd,raw[offset:])
            except BlockingIOError:continue
            if not count:raise EOFError('Session output closed')
            offset+=count
    finally:os.set_blocking(fd,previous)


class Receiver:
    def __init__(self,nonce,clock=time.monotonic,timeout=10,backlog=None):
        # timeout bounds silence between messages; backlog bounds how far the producer's emission
        # timeline may run ahead of receipt. A whole snapshot enters the ssh channel at once while the
        # relay acknowledges it one message at a time with a CP round trip each, so the lag grows with
        # the session count (1,801 sessions took about 25 s on the PA-5220); the default keeps both equal.
        self.nonce=canonical_uuid(nonce);self.clock=clock;self.timeout=timeout
        self.backlog=timeout if backlog is None else backlog
        self.producer=None;self.policy=None;self.limits=None;self.sequence=0;self.sessions={}
        # since: receiver clock at which each identity entered the inventory (its age for readers).
        # candidates/l3_candidates are maintained incrementally so status() stays O(1) per message.
        self.since={};self.candidates=0;self.l3_candidates=0;self.changes=0
        self.ready=False;self.last=None;self.reason='awaiting snapshot';self.messages=0
        self.origin=None;self.remote_origin=None;self.remote_last=None

    def fence(self,reason):
        self.ready=False;self.sessions.clear();self.since.clear();self.candidates=self.l3_candidates=0
        self.changes+=1;self.reason=str(reason)[:256]

    def tick(self):
        now=self.clock()
        if (type(now) not in (int,float) or not math.isfinite(now) or now<0 or
                self.last is not None and not 0<=now-self.last<self.timeout):
            self.fence('Session stream expired or clock changed')
            raise TimeoutError(self.reason)
        return now

    def _store(self,row,now):
        identity=row['identity'];previous=self.sessions.get(identity)
        if previous is not None:
            self.candidates-=previous['software_candidate'];self.l3_candidates-=(previous.get('l3') or {}).get('available') is True
        self.sessions[identity]=copy.deepcopy(row);self.since.setdefault(identity,now)
        self.candidates+=row['software_candidate'];self.l3_candidates+=(row.get('l3') or {}).get('available') is True
        self.changes+=1

    def _drop(self,identity):
        previous=self.sessions.pop(identity,None);self.since.pop(identity,None)
        if previous is not None:
            self.candidates-=previous['software_candidate'];self.l3_candidates-=(previous.get('l3') or {}).get('available') is True
            self.changes+=1

    def _apply(self,op,payload,now):
        if op=='upsert':
            self.validate_row(payload);validate_l3(payload,self.policy);self._store(payload,now)
        elif op=='close':
            if not isinstance(payload,dict) or set(payload)!={'identity'}:raise ValueError('Invalid session close')
            sha(payload['identity']);self._drop(payload['identity'])
        else:raise ValueError('Unknown session change')

    def accept(self,message):
        try:
            now=self.tick()
            if set(message)=={'schema','nonce','unavailable'}:
                if (type(message['schema']) is not int or message['schema']!=1 or message['nonce']!=self.nonce or
                        not isinstance(message['unavailable'],str) or not 1<=len(message['unavailable'])<=512):
                    raise ValueError('Invalid unavailable acknowledgement')
                self.fence(message['unavailable']);self.producer=None;self.policy=None;self.limits=None;self.sequence=0
                self.origin=None;self.remote_origin=None;self.remote_last=None
                self.last=now;self.messages+=1
                return self.status()
            if (set(message)!={'schema','nonce','producer','sequence','operation','payload','emitted_monotonic'} or
                    type(message['schema']) is not int or message['schema']!=1 or message['nonce']!=self.nonce):
                raise ValueError('Invalid session stream envelope')
            p=message['producer'];seq=message['sequence'];op=message['operation'];payload=message['payload']
            emitted=message['emitted_monotonic']
            if type(emitted) not in (int,float) or not math.isfinite(emitted) or emitted<0:
                raise ValueError('Invalid producer timestamp')
            if (not isinstance(p,dict) or set(p)!={'boot_id','pid','process_start','stream_id'} or
                    type(p['pid']) is not int or not 0<p['pid']<2**31 or
                    not isinstance(p['process_start'],str) or not p['process_start'].isascii() or
                    not p['process_start'].isdigit() or int(p['process_start'])<=0):
                raise ValueError('Invalid DP producer identity')
            canonical_uuid(p['boot_id']);canonical_uuid(p['stream_id'])
            if type(seq) is not int or not 1<=seq<2**63:raise ValueError('Invalid stream sequence')
            if op=='begin':
                if seq!=1 or p==self.producer:raise ValueError('Replayed snapshot start')
                self.fence('receiving snapshot')
                required={'revision','digest','nat_digest','bindings','collector'}
                if (not isinstance(payload,dict) or not required<=set(payload) or set(payload)-required-{'l3','limits'} or
                        type(payload['revision']) is not int or not 0<=payload['revision']<2**63 or
                        not isinstance(payload['bindings'],dict) or not isinstance(payload['collector'],dict)):
                    raise ValueError('Invalid acknowledged policy context')
                sha(payload['digest']);sha(payload['nat_digest'])
                if 'l3' in payload:
                    context=payload['l3']
                    if (not isinstance(context,dict) or set(context)!={'snapshot_digest','hardware_admission'} or
                            context['hardware_admission'] is not False):raise ValueError('Invalid L3 context')
                    sha(context['snapshot_digest'])
                if 'limits' in payload:validate_limits(payload['limits'])
                self.producer=copy.deepcopy(p);self.sequence=0
                self.policy={k:copy.deepcopy(v) for k,v in payload.items() if k!='limits'}
                self.limits=copy.deepcopy(payload.get('limits'))
                self.origin=now;self.remote_origin=emitted;self.remote_last=emitted
            elif p!=self.producer or seq!=self.sequence+1:
                raise ValueError('DP stream identity or sequence changed')
            if (emitted<self.remote_last or
                    abs((now-self.origin)-(emitted-self.remote_origin))>=self.backlog):
                raise ValueError('DP stream backlog or producer clock changed')
            if op=='snapshot':
                if self.ready or not isinstance(payload,list) or not 1<=len(payload)<=BATCH:
                    raise ValueError('Invalid snapshot chunk')
                for row in payload:
                    self.validate_row(row);validate_l3(row,self.policy)
                    if row['identity'] in self.sessions:raise ValueError('Duplicate snapshot identity')
                    self._store(row,now)
            elif op=='synchronized':
                if self.ready or payload!={}:raise ValueError('Invalid snapshot completion')
                self.ready=True;self.reason=None;self.changes+=1
            elif op in ('upsert','close','heartbeat'):
                if not self.ready:raise ValueError('Snapshot is not complete')
                if op=='heartbeat':
                    if payload!={}:raise ValueError('Invalid heartbeat')
                else:self._apply(op,payload,now)
            elif op=='changes':
                # One conntrack burst: ordered upserts and closes, applied as a unit.
                if not self.ready:raise ValueError('Snapshot is not complete')
                if not isinstance(payload,list) or not 1<=len(payload)<=BATCH:raise ValueError('Invalid change batch')
                for entry in payload:
                    if not isinstance(entry,dict) or not isinstance(entry.get('op'),str):raise ValueError('Invalid session change')
                    if entry['op']=='upsert':
                        if set(entry)!={'op','row'}:raise ValueError('Invalid session change')
                        self._apply('upsert',entry['row'],now)
                    elif entry['op']=='close':
                        if set(entry)!={'op','identity'}:raise ValueError('Invalid session change')
                        self._apply('close',{'identity':entry['identity']},now)
                    else:raise ValueError('Unknown session change')
            elif op!='begin':raise ValueError('Unknown session operation')
            if len(self.sessions)>CAPACITY:raise ValueError('Session inventory capacity exceeded')
            self.sequence=seq;self.last=now;self.remote_last=emitted;self.messages+=1
            return self.status()
        except Exception as error:
            self.fence(error);raise

    @staticmethod
    def validate_row(row):
        if (not isinstance(row,dict) or type(row.get('software_candidate')) is not bool or
                not isinstance(row.get('blockers'),list) or
                any(not isinstance(v,str) for v in row['blockers']) or
                row['software_candidate']!=(not row['blockers']) or
                not isinstance(row.get('original'),dict) or not isinstance(row.get('reply'),dict)):
            raise ValueError('Invalid assessed session')
        sha(row['identity'])

    def status(self):
        return dict(schema=1,mode='observation-only',hardware_admission=False,ready=self.ready,
                    producer=copy.deepcopy(self.producer),policy=copy.deepcopy(self.policy),
                    limits=copy.deepcopy(self.limits),
                    sequence=self.sequence,messages=self.messages,sessions=len(self.sessions),
                    l3_observed=self.ready and 'l3' in (self.policy or {}),
                    l3_candidates=self.l3_candidates,software_candidates=self.candidates,reason=self.reason)

    def inventory(self):
        """Compact rows for readers; an inventory that is not ready is empty, never partial."""
        if not self.ready:return []
        return [compact(row,self.since.get(identity)) for identity,row in self.sessions.items()]


def compact(row,since=None):
    """The browser's view of one assessed session: tuples, rule, state, counters and offload verdicts."""
    original=row.get('original') or {};translated=row.get('translated') or {};nat=row.get('nat') or {}
    counters=row.get('counters') or {};rule=row.get('rule') or {};l3=row.get('l3')
    def counter(direction,field):
        value=(counters.get(direction) or {}).get(field)
        return value if type(value) is int else None
    value=dict(id=row['identity'],ct=row.get('conntrack_id'),proto=original.get('protocol'),
               src=original.get('source'),sport=original.get('source_port'),
               dst=original.get('destination'),dport=original.get('destination_port'),
               nat=dict(source=nat.get('source') is True,destination=nat.get('destination') is True),
               rule=rule.get('name') if isinstance(rule,dict) else None,
               vsys=rule.get('scope') if isinstance(rule,dict) else None,
               state=row.get('tcp_state'),ttl=row.get('remaining_seconds'),
               bytes=dict(c2s=counter('original','bytes'),s2c=counter('reply','bytes')),
               packets=dict(c2s=counter('original','packets'),s2c=counter('reply','packets')),
               candidate=row.get('software_candidate') is True,blockers=list(row.get('blockers') or []),
               l3=(dict(available=l3.get('available') is True,pair=l3.get('pair'),
                        blockers=list(l3.get('blockers') or [])) if isinstance(l3,dict) else None),
               since=since)
    if value['nat']['source'] or value['nat']['destination']:
        value.update(xsrc=translated.get('source'),xsport=translated.get('source_port'),
                     xdst=translated.get('destination'),xdport=translated.get('destination_port'))
    return value


def local_identity():
    return dict(boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),pid=os.getpid(),
                process_start=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19])


def acknowledgement(nonce,state):
    value=dict(schema=1,nonce=nonce,producer=state['producer'],sequence=state['sequence'],
               ready=state['ready'],sessions=state['sessions'],hardware_admission=False)
    context=(state.get('policy') or {}).get('l3')
    if context is not None:
        value['l3']=dict(snapshot_digest=context['snapshot_digest'],observed=state['l3_observed'],
                         candidates=state['l3_candidates'])
    return value


def _report(path,limit):
    path=Path(path);st=path.stat()
    # Written by root on the appliance; a reader may also trust its own file (tests, tooling).
    if path.is_symlink() or st.st_uid not in (0,os.geteuid()) or st.st_mode&0o022 or st.st_size>limit:
        raise ValueError('Untrusted report')
    value=json.loads(path.read_text());writer=value['writer']
    process=Path('/proc/'+str(writer['pid'])+'/stat').read_text().rsplit(')',1)[1].split()
    alive=(writer['boot_id']==local_identity()['boot_id'] and process[19]==writer['process_start'] and process[0]!='Z')
    return value,alive


def read_status(path):
    """Never report a dead/restarted writer or stale snapshot as synchronized."""
    unavailable=dict(available=False,fresh=False,ready=False,hardware_admission=False)
    try:
        value,alive=_report(path,MAX_FRAME)
        fresh=(value['schema']==1 and value['hardware_admission'] is False and
               0<=time.monotonic()-value['monotonic_time']<10 and alive)
        return dict(value,available=True,fresh=fresh,ready=fresh and value['ready'],
                    l3_observed=fresh and value['ready'] and value.get('l3_observed',False),
                    hardware_admission=False)
    except (OSError,ValueError,KeyError,TypeError,IndexError):return unavailable


def read_inventory(path):
    """The relay's compact inventory; rows are reported only from a live writer's recent, ready report."""
    unavailable=dict(available=False,fresh=False,ready=False,hardware_admission=False,sessions=[])
    try:
        value,alive=_report(path,INVENTORY_BYTES)
        if value['schema']!=1 or value['hardware_admission'] is not False or not isinstance(value['sessions'],list):
            raise ValueError('Invalid inventory report')
        age=time.monotonic()-value['monotonic_time']
        fresh=0<=age<INVENTORY_FRESH_SECONDS and alive
        ready=fresh and value['ready'] is True
        return dict(value,available=True,fresh=fresh,ready=ready,age_seconds=round(max(age,0),3),
                    sessions=value['sessions'] if ready else [],hardware_admission=False)
    except (OSError,ValueError,KeyError,TypeError,IndexError):return unavailable
