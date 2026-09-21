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
                 maximum_lifetime=300, capacity=4096, clock=time.monotonic):
        for value in (heartbeat_timeout,idle_timeout,maximum_lifetime):
            if type(value) not in (int,float) or not math.isfinite(value) or value<=0:
                raise ValueError('positive finite lifecycle timeouts required')
        if type(capacity) is not int or not 1<=capacity<=1000000:
            raise ValueError('invalid session capacity')
        self.owner,self.clock=owner,clock
        self.heartbeat_timeout,self.idle_timeout=heartbeat_timeout,idle_timeout
        self.maximum_lifetime,self.capacity=maximum_lifetime,capacity
        self.producer=None;self.sequence=0;self.last_heartbeat=None;self.leases={}
        self.reason='producer not synchronized'

    def fence(self, reason):
        # Revoke activation before any hardware I/O, including failed cleanup.
        self.producer=None;self.last_heartbeat=None;self.reason=reason
        self.owner.activated=False
        self.owner.reconcile()
        self.leases.clear()

    def start(self, boot_id, revision, policy_digest):
        if not isinstance(boot_id,str) or str(uuid.UUID(boot_id))!=boot_id:
            raise ValueError('canonical producer boot UUID required')
        self.fence('producer synchronization')
        # Existing owner checks exact applied revision/digest and live bindings.
        self.owner.activate(revision,policy_digest)
        self.producer=boot_id;self.sequence=0;self.last_heartbeat=self.clock()
        self.reason=None
        return self.status()

    def tick(self):
        now=self.clock()
        if self.producer is None:
            self.owner.reconcile()
            return self.status()
        if now<self.last_heartbeat or now-self.last_heartbeat>=self.heartbeat_timeout:
            self.fence('producer heartbeat expired')
            return self.status()
        self.owner.reconcile()
        if not self.owner.status()['admission_enabled']:
            self.fence('policy, attachment or qualification changed')
            return self.status()
        try:
            for ident,lease in list(self.leases.items()):
                if now>=lease['expires'] or now>=lease['deadline']:
                    self.owner.revoke(ident)
                    del self.leases[ident]
        except Exception:
            self.fence('session removal not acknowledged')
            raise
        return self.status()

    def event(self, boot_id, sequence, operation, payload):
        self.tick()
        if self.producer is None:raise RuntimeError('session producer is not synchronized')
        uint(sequence,64,'producer sequence')
        if boot_id!=self.producer or sequence!=self.sequence+1:
            self.fence('producer identity or event sequence changed')
            raise RuntimeError('session stream lost continuity; resynchronize')
        if operation not in ('heartbeat','open','refresh','close') or not isinstance(payload,dict):
            self.fence('invalid session event')
            raise ValueError('invalid session event')
        try:
            if operation=='heartbeat':
                if payload:raise ValueError('heartbeat has no payload')
                self.last_heartbeat=self.clock()
            elif operation=='open':
                if len(self.leases)>=self.capacity:raise ValueError('offload session capacity reached')
                self.owner.admit(payload)
                now=self.clock()
                self.leases[payload['session_id']]={'decision':digest(payload),'expires':now+self.idle_timeout,
                                                  'deadline':now+self.maximum_lifetime}
            elif operation=='refresh':
                if set(payload)!={'session_id','decision_digest'}:raise ValueError('invalid refresh')
                ident=uint(payload['session_id'],31,'session ID')
                lease=self.leases.get(ident)
                if lease is None or payload['decision_digest']!=lease['decision']:
                    raise ValueError('session decision must be reevaluated')
                lease['expires']=min(self.clock()+self.idle_timeout,lease['deadline'])
            else:
                if set(payload)!={'session_id'}:raise ValueError('invalid close')
                ident=uint(payload['session_id'],31,'session ID')
                self.owner.revoke(ident);self.leases.pop(ident,None)
            self.sequence=sequence
        except Exception:
            self.fence('session event failed; reevaluation required')
            raise
        return self.status()

    def status(self):
        return dict(producer_boot_id=self.producer,sequence=self.sequence,leases=len(self.leases),
                    synchronized=self.producer is not None,reason=self.reason,
                    policy=self.owner.status())
