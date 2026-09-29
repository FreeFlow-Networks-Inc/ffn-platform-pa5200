"""Bounded DP session stream framing and observation-only CP receiver.

This module cannot program hardware. Its transport must authenticate both
endpoints; nonce/sequence checks supply continuity, not authentication.
"""
import copy
import json
import math
import os
from pathlib import Path
import select
import time
import uuid

MAX_FRAME=524288
CAPACITY=8192


def canonical_uuid(value):
    if not isinstance(value,str) or str(uuid.UUID(value))!=value:raise ValueError('Canonical UUID required')
    return value


def sha(value):
    if not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('SHA256 identity required')


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
    def __init__(self,nonce,clock=time.monotonic,timeout=10):
        self.nonce=canonical_uuid(nonce);self.clock=clock;self.timeout=timeout
        self.producer=None;self.policy=None;self.sequence=0;self.sessions={}
        self.ready=False;self.last=None;self.reason='awaiting snapshot';self.messages=0
        self.origin=None;self.remote_origin=None;self.remote_last=None

    def fence(self,reason):
        self.ready=False;self.sessions.clear();self.reason=str(reason)[:256]

    def tick(self):
        now=self.clock()
        if (type(now) not in (int,float) or not math.isfinite(now) or now<0 or
                self.last is not None and not 0<=now-self.last<self.timeout):
            self.fence('Session stream expired or clock changed')
            raise TimeoutError(self.reason)
        return now

    def accept(self,message):
        try:
            now=self.tick()
            if set(message)=={'schema','nonce','unavailable'}:
                if (type(message['schema']) is not int or message['schema']!=1 or message['nonce']!=self.nonce or
                        not isinstance(message['unavailable'],str) or not 1<=len(message['unavailable'])<=512):
                    raise ValueError('Invalid unavailable acknowledgement')
                self.fence(message['unavailable']);self.producer=None;self.policy=None;self.sequence=0
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
                if (not isinstance(payload,dict) or set(payload)!={'revision','digest','nat_digest','bindings','collector'} or
                        type(payload['revision']) is not int or not 0<=payload['revision']<2**63 or
                        not isinstance(payload['bindings'],dict) or not isinstance(payload['collector'],dict)):
                    raise ValueError('Invalid acknowledged policy context')
                sha(payload['digest']);sha(payload['nat_digest'])
                self.producer=copy.deepcopy(p);self.policy=copy.deepcopy(payload);self.sequence=0
                self.origin=now;self.remote_origin=emitted;self.remote_last=emitted
            elif p!=self.producer or seq!=self.sequence+1:
                raise ValueError('DP stream identity or sequence changed')
            if (emitted<self.remote_last or
                    abs((now-self.origin)-(emitted-self.remote_origin))>=self.timeout):
                raise ValueError('DP stream backlog or producer clock changed')
            if op=='snapshot':
                if self.ready or not isinstance(payload,list) or not 1<=len(payload)<=128:
                    raise ValueError('Invalid snapshot chunk')
                for row in payload:
                    self.validate_row(row)
                    if row['identity'] in self.sessions:raise ValueError('Duplicate snapshot identity')
                    self.sessions[row['identity']]=copy.deepcopy(row)
            elif op=='synchronized':
                if self.ready or payload!={}:raise ValueError('Invalid snapshot completion')
                self.ready=True;self.reason=None
            elif op in ('upsert','close','heartbeat'):
                if not self.ready:raise ValueError('Snapshot is not complete')
                if op=='upsert':
                    self.validate_row(payload);self.sessions[payload['identity']]=copy.deepcopy(payload)
                elif op=='close':
                    if not isinstance(payload,dict) or set(payload)!={'identity'}:raise ValueError('Invalid session close')
                    sha(payload['identity']);self.sessions.pop(payload['identity'],None)
                elif payload!={}:raise ValueError('Invalid heartbeat')
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
                    sequence=self.sequence,messages=self.messages,sessions=len(self.sessions),
                    software_candidates=sum(r['software_candidate'] for r in self.sessions.values()),reason=self.reason)


def local_identity():
    return dict(boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),pid=os.getpid(),
                process_start=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19])


def read_status(path):
    """Never report a dead/restarted writer or stale snapshot as synchronized."""
    unavailable=dict(available=False,fresh=False,ready=False,hardware_admission=False)
    try:
        path=Path(path);st=path.stat()
        if path.is_symlink() or st.st_uid!=0 or st.st_mode&0o022 or st.st_size>MAX_FRAME:
            return unavailable
        value=json.loads(path.read_text());writer=value['writer']
        process=Path('/proc/'+str(writer['pid'])+'/stat').read_text().rsplit(')',1)[1].split()
        fresh=(value['schema']==1 and value['hardware_admission'] is False and
               0<=time.monotonic()-value['monotonic_time']<10 and
               writer['boot_id']==local_identity()['boot_id'] and
               process[19]==writer['process_start'] and process[0]!='Z')
        return dict(value,available=True,fresh=fresh,ready=fresh and value['ready'],hardware_admission=False)
    except (OSError,ValueError,KeyError,TypeError,IndexError):return unavailable
