#!/usr/bin/env python3
"""Policy admission/invalidation for a serialized FE100 session owner.

The trusted policy evaluator supplies verdicts; this module does not evaluate
firewall rules or infer that a TCP connection is established. A policy or
attachment change drains both directions before the new generation can admit.
State persistence must be atomic/durable and share the session owner's lock.
"""
import hashlib
import json
from ffn_fe100_sessions import key4, forwarding_entry4, uint


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


class PolicyOwner:
    def __init__(self, sessions, load, save, bindings, qualified):
        self.sessions,self.save,self.bindings,self.qualified=sessions,save,bindings,qualified
        self.state=load() or {'revision':0,'phase':'blocked','digest':None,'attachment':None}
        if (set(self.state)!={'revision','phase','digest','attachment'} or
                self.state['phase'] not in ('blocked','draining','active')):
            raise ValueError('invalid policy journal')
        uint(self.state['revision'],64,'policy revision')
        # Persisted activation is intent, not a live owner's acknowledgement.
        self.activated = False

    def reconcile(self):
        """Fence and drain stale ownership, even when no new flows arrive.

        Call periodically under the journal and adapter locks. Recovery never
        activates a policy or increments its generation. Exact ownership and
        deletion readback remain the SessionManager's responsibility.
        """
        stale = self.state['phase'] != 'active' or not self.activated
        if not stale:
            try:
                stale = (self.qualified() is not True or
                         digest(self.bindings()) != self.state['attachment'])
            except Exception:
                stale = True
        if stale or self.sessions.recovery_required:
            self.activated = False
            if (self.state['phase'] != 'blocked' or self.state['attachment'] is not None or
                    self.sessions.sessions or self.sessions.recovery_required):
                self.persist(phase='draining', attachment=None)
                self.sessions.recover()
                self.persist(phase='blocked')
        return self.status()

    def persist(self, **changes):
        wanted=self.state | changes
        try:self.save(wanted)
        except BaseException:
            self.state=self.state | {'phase':'blocked'}
            raise
        self.state=wanted

    def replace(self, expected_revision, policy_digest):
        """Call before applying policy/network/inspection changes, never after."""
        uint(expected_revision,64,'policy revision')
        if expected_revision!=self.state['revision']:raise ValueError('revision conflict')
        if (not isinstance(policy_digest,str) or len(policy_digest)!=64 or
                any(c not in '0123456789abcdef' for c in policy_digest)):
            raise ValueError('SHA256 policy digest required')
        revision=uint(expected_revision+1,64,'policy revision')
        self.activated = False
        self.persist(revision=revision,phase='draining',digest=policy_digest,attachment=None)
        # Remove all owned entries, including an interrupted prior generation.
        # recover verifies exact ownership; failures keep admissions blocked.
        self.sessions.recover()
        self.persist(phase='blocked')
        return self.status()

    def activate(self, revision, policy_digest):
        """Only after software policy apply and current attachment verification."""
        uint(revision,64,'policy revision')
        if (revision==0 or not isinstance(policy_digest,str) or len(policy_digest)!=64 or
                any(c not in '0123456789abcdef' for c in policy_digest)):
            raise ValueError('an applied policy generation is required')
        if revision!=self.state['revision'] or policy_digest!=self.state['digest']:
            raise ValueError('stale applied policy acknowledgement')
        if self.state['phase']!='blocked' or self.sessions.recovery_required:
            raise RuntimeError('session recovery incomplete')
        if self.qualified() is not True:raise RuntimeError('front-port offload is not qualified')
        self.persist(phase='active',attachment=digest(self.bindings()))
        self.activated = True

    def admit(self, request):
        fields={'session_id','revision','policy_digest','rule_id','verdict','protocol','src','dst',
                'sport','dport','zone','ingress','egress','inspection_required','nat_required','established'}
        if not isinstance(request,dict) or set(request)!=fields:raise ValueError('invalid admission fields')
        uint(request['revision'],64,'policy revision')
        if not self.activated or self.state['phase']!='active' or self.sessions.recovery_required:
            raise RuntimeError('policy admission blocked')
        bindings=self.bindings()
        if digest(bindings)!=self.state['attachment'] or self.qualified() is not True:
            self.persist(phase='draining')
            self.sessions.recover();self.persist(phase='blocked',attachment=None)
            raise RuntimeError('attachment or qualification changed; sessions drained')
        if request['revision']!=self.state['revision'] or request['policy_digest']!=self.state['digest']:
            raise ValueError('stale policy decision')
        if (request['verdict']!='allow' or not isinstance(request['rule_id'],str) or not 1<=len(request['rule_id'])<=128 or
                request['inspection_required'] is not False or request['nat_required'] is not False or
                request['established'] is not True):
            raise ValueError('flow requires software enforcement')
        ports=(request['ingress'],request['egress'])
        if any(type(p) is not int or str(p) not in bindings for p in ports) or ports[0]==ports[1]:
            raise ValueError('verified distinct front-port bindings required')
        hops=[]
        for port in reversed(ports):
            b=bindings[str(port)]
            if b.get('enabled') is not True or b.get('link') is not True:
                raise RuntimeError('egress attachment is unavailable')
            hops.append(uint(b['next_hop'],16,'next-hop'))
        sid=uint(request['session_id'],31,'session ID')
        k=key4(request['src'],request['dst'],request['sport'],request['dport'],request['protocol'],request['zone'])
        reverse=key4(request['dst'],request['src'],request['dport'],request['sport'],request['protocol'],request['zone'])
        entries=[forwarding_entry4(key,sid*2+i,hop) for i,(key,hop) in enumerate(zip((k,reverse),hops))]
        self.sessions.install(sid,entries,self.state['revision'])
        return {'installed':True,'session_id':sid,'revision':self.state['revision'],'directions':2}

    def revoke(self, session_id):
        uint(session_id,31,'session ID')
        if session_id in self.sessions.sessions:self.sessions.remove(session_id)
        return {'removed':True,'session_id':session_id}

    def status(self):
        return self.state | {'sessions':len(self.sessions.sessions),
            'recovery_required':self.sessions.recovery_required,
            'admission_enabled':self.activated and self.state['phase']=='active' and not self.sessions.recovery_required}
