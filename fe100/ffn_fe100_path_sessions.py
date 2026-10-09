"""Couple paired FE100 session lifetime to durable hardware path resources.

Internal trusted-evaluator component. Does not activate policies or turn DP
observations into allow verdicts. Run under the same journal and table locks,
and call reconcile periodically alongside the producer's lease timer.
"""
import copy
from ffn_fe100_policy import digest
from ffn_fe100_sessions import uint


class PathSessions:
    def __init__(self,policy,resources):
        self.policy,self.resources=policy,resources
        policy.paths=self.lookup
        resources.drain=self.drain

    @staticmethod
    def key(request):
        uint(request['session_id'],31,'session ID');uint(request['revision'],64,'revision')
        return digest({k:request[k] for k in ('session_id','revision','policy_digest')})

    def lookup(self,request):return self.resources.snapshot(self.key(request))

    def drain(self,key):
        # After restart, lost in-memory dependency indexes cannot prove absence
        # of references. Drain durable session intent before reclaiming paths.
        sessions=self.policy.sessions
        if sessions.recovery_required or set(sessions.sessions)-set(self.policy.dependencies):
            self.policy.activated=False;self.policy.reconcile()
        for sid,request in list(self.policy.dependencies.items()):
            if self.key(request)==key:self.policy.revoke(sid)
        return not sessions.recovery_required and not any(self.key(r)==key for r in self.policy.dependencies.values())

    def admit(self,request,plan):
        if not isinstance(request,dict) or 'path_digest' in request:raise ValueError('Path digest is assigned by the resource owner')
        request=copy.deepcopy(request);key=self.key(request)
        self.policy.reconcile()
        if not self.policy.status()['admission_enabled']:raise RuntimeError('Policy admission blocked')
        if request['session_id'] in self.policy.sessions.sessions:raise ValueError('Session already owned')
        if (request['revision']!=self.policy.state['revision'] or request['policy_digest']!=self.policy.state['digest'] or
            request.get('verdict')!='allow' or request.get('inspection_required') is not False or
            request.get('established') is not True or type(request.get('nat_required')) is not bool):
            raise ValueError('Current explicit software decision required')
        if request['nat_required'] and self.policy.nat_qualified() is not True:raise RuntimeError('NAT qualification required')
        # The independent trusted path resolver is checked before/after every
        # table write by PathOwner. PolicyOwner then verifies tuple directions.
        try:
            snapshot=self.resources.acquire(key,plan)
            request['path_digest']=digest(snapshot)
            return self.policy.admit(request)
        except BaseException:
            # A partial session write remains durable until its exact removal
            # is acknowledged. Never reclaim its next hops before that drain.
            self.policy.activated=False
            self.policy.reconcile()
            self.resources.release(key)
            raise

    def revoke(self,session_id):
        request=self.policy.dependencies.get(session_id)
        try:
            self.policy.revoke(session_id)
            if request is not None:self.resources.release(self.key(request))
        except BaseException:
            self.policy.activated=False
            raise

    def reconcile(self):
        try:
            if self.resources.recovery_required:self.policy.activated=False
            self.policy.reconcile()
            self.resources.reconcile()
            live={self.key(r) for r in self.policy.dependencies.values()}
            for key in list(self.resources.paths):
                if key not in live:self.resources.release(key)
        except BaseException:
            self.policy.activated=False
            raise
        return self.status()

    def status(self):
        return dict(policy=self.policy.status(),paths=len(self.resources.paths),
                    path_recovery_required=self.resources.recovery_required)

    def recover(self):
        self.policy.activated=False;self.policy.reconcile()
        self.resources.recover()
