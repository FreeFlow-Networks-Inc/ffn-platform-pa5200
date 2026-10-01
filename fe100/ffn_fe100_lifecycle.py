#!/usr/bin/env python3
"""Bounded session leases for a serialized, trusted dataplane evaluator.

This is an internal owner component, not a CLI or network admission endpoint.
The caller must authenticate the producer, verify its applied generation, and
call tick on a monotonic timer while holding the journal/adapter owner locks.
Loss of producer continuity drains exact owned entries before reactivation.
"""
import math
import time
import uuid
from ffn_fe100_policy import digest
from ffn_fe100_sessions import uint


class SessionLifecycle:
    def __init__(self, owner, *, heartbeat_timeout=15, idle_timeout=60,
                 maximum_lifetime=300, capacity=4096, clock=time.monotonic,
                 paths=None, generation=None):
        for value in (heartbeat_timeout,idle_timeout,maximum_lifetime):
            if type(value) not in (int,float) or not math.isfinite(value) or value<=0:
                raise ValueError('positive finite lifecycle timeouts required')
        if type(capacity) is not int or not 1<=capacity<=1000000:
            raise ValueError('invalid session capacity')
        self.owner,self.clock=owner,clock
        if paths is not None and (paths.policy is not owner or not callable(generation)):
            raise ValueError('Path lifecycle requires the same policy owner and a trusted generation reader')
        if generation is not None and not callable(generation):
            raise ValueError('Trusted generation reader required')
        self.paths,self.read_generation=paths,generation
        self.generation_digest=None
        self.heartbeat_timeout,self.idle_timeout=heartbeat_timeout,idle_timeout
        self.maximum_lifetime,self.capacity=maximum_lifetime,capacity
        self.producer=None;self.sequence=0;self.last_heartbeat=None;self.leases={}
        self.last_clock=None
        self.reason='producer not synchronized'

    def fence(self, reason):
        # Revoke activation before any hardware I/O, including failed cleanup.
        self.producer=None;self.last_heartbeat=None;self.reason=reason
        self.generation_digest=None
        self.owner.activated=False
        self.reconcile()
        self.leases.clear()

    def reconcile(self):
        return self.paths.reconcile() if self.paths is not None else self.owner.reconcile()

    def revoke(self, ident):
        return self.paths.revoke(ident) if self.paths is not None else self.owner.revoke(ident)

    def current_generation(self):
        if self.read_generation is None:return None
        value=self.read_generation()
        if not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
            raise RuntimeError('Current applied topology/policy generation unavailable')
        return value

    @staticmethod
    def identity(producer):
        if not isinstance(producer,dict) or set(producer)!={'boot_id','pid','process_start','stream_id'}:
            raise ValueError('complete authenticated producer identity required')
        for key in ('boot_id','stream_id'):
            value=producer[key]
            if not isinstance(value,str) or str(uuid.UUID(value))!=value:
                raise ValueError('canonical producer UUID required')
        if (type(producer['pid']) is not int or not 0<producer['pid']<2**31 or
                not isinstance(producer['process_start'],str) or
                not producer['process_start'].isascii() or not producer['process_start'].isdigit() or
                int(producer['process_start'])<=0):
            raise ValueError('producer PID and process start required')
        return dict(producer)

    def now(self):
        value=self.clock()
        if (type(value) not in (int,float) or not math.isfinite(value) or value<0 or
                self.last_clock is not None and value<self.last_clock):
            raise RuntimeError('invalid or regressed monotonic clock')
        self.last_clock=value
        return value

    def start(self, producer, revision, policy_digest):
        self.fence('producer synchronization')
        producer=self.identity(producer)
        now=self.now()
        try:
            generation=self.current_generation()
            # Existing owner checks exact applied revision/digest and live bindings.
            self.owner.activate(revision,policy_digest)
            if self.current_generation()!=generation:
                raise RuntimeError('Generation changed during activation')
        except Exception:
            self.fence('generation activation failed')
            raise
        self.generation_digest=generation
        self.producer=producer;self.sequence=0;self.last_heartbeat=now
        self.reason=None
        result=self.tick()
        if self.producer is None:raise RuntimeError('Producer lease expired during activation')
        return result

    def tick(self):
        try:now=self.now()
        except Exception:
            self.fence('invalid producer lease clock')
            raise
        if self.producer is None:
            self.reconcile()
            return self.status()
        if now<self.last_heartbeat or now-self.last_heartbeat>=self.heartbeat_timeout:
            self.fence('producer heartbeat expired')
            return self.status()
        try:
            if self.current_generation()!=self.generation_digest:
                self.fence('applied topology or policy generation changed')
                return self.status()
            self.reconcile()
            # Reconciliation itself reads native tables and may take time.
            # Its successful readback cannot renew an expired producer or
            # acknowledge a generation that changed during those reads.
            now=self.now()
            if self.current_generation()!=self.generation_digest:
                self.fence('generation changed during reconciliation')
                return self.status()
            if now-self.last_heartbeat>=self.heartbeat_timeout:
                self.fence('producer heartbeat expired during reconciliation')
                return self.status()
        except Exception:
            self.producer=None;self.last_heartbeat=None
            self.owner.activated=False;self.reason='dependency recovery not acknowledged'
            self.generation_digest=None
            # An unavailable generation reader must drain existing entries too.
            self.reconcile()
            raise
        if not self.owner.status()['admission_enabled']:
            self.fence('policy, attachment or qualification changed')
            return self.status()
        try:
            for ident,lease in list(self.leases.items()):
                if now>=lease['expires'] or now>=lease['deadline']:
                    self.revoke(ident)
                    del self.leases[ident]
        except Exception:
            self.fence('session removal not acknowledged')
            raise
        return self.status()

    def event(self, producer, sequence, operation, payload):
        self.tick()
        if self.producer is None:raise RuntimeError('session producer is not synchronized')
        try:
            producer=self.identity(producer)
            uint(sequence,64,'producer sequence')
            if producer!=self.producer or sequence!=self.sequence+1:
                raise RuntimeError('session stream lost continuity; resynchronize')
            if operation not in ('heartbeat','open','refresh','close') or not isinstance(payload,dict):
                raise ValueError('invalid session event')
            if operation=='heartbeat':
                if payload:raise ValueError('heartbeat has no payload')
                self.last_heartbeat=self.now()
            elif operation=='open':
                if len(self.leases)>=self.capacity:raise ValueError('offload session capacity reached')
                if payload.get('session_id') in self.leases:raise ValueError('session already leased')
                if self.paths is None:self.owner.admit(payload)
                else:
                    # Resolve commissioned resources locally. The event cannot
                    # choose hardware indices or provide its own path digest.
                    plan=self.paths.resources.current(self.paths.key(payload))
                    self.paths.admit(payload,plan)
                now=self.now()
                self.leases[payload['session_id']]={'decision':digest(payload),'expires':now+self.idle_timeout,
                                                  'deadline':now+self.maximum_lifetime}
            elif operation=='refresh':
                if set(payload)!={'session_id','decision_digest'}:raise ValueError('invalid refresh')
                ident=uint(payload['session_id'],31,'session ID')
                lease=self.leases.get(ident)
                if lease is None or payload['decision_digest']!=lease['decision']:
                    raise ValueError('session decision must be reevaluated')
                lease['expires']=min(self.now()+self.idle_timeout,lease['deadline'])
            else:
                if set(payload)!={'session_id'}:raise ValueError('invalid close')
                ident=uint(payload['session_id'],31,'session ID')
                self.revoke(ident);self.leases.pop(ident,None)
            # Native table calls can span a generation change or lease expiry.
            # Do not acknowledge an event until post-write reconciliation.
            self.tick()
            if self.producer is None:raise RuntimeError('Generation or producer lease changed during event')
            self.sequence=sequence
        except Exception:
            self.fence('session event failed; reevaluation required')
            raise
        return self.status()

    def status(self):
        return dict(producer_boot_id=self.producer['boot_id'] if self.producer else None,
                    producer=dict(self.producer) if self.producer else None,
                    sequence=self.sequence,leases=len(self.leases),
                    synchronized=self.producer is not None,reason=self.reason,
                    generation_digest=self.generation_digest,
                    paths=self.paths.status() if self.paths is not None else None,
                    policy=self.owner.status())
