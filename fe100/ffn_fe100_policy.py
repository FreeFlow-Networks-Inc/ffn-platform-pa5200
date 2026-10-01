#!/usr/bin/env python3
"""Policy admission/invalidation for a serialized FE100 session owner.

The trusted policy evaluator supplies verdicts; this module does not evaluate
firewall rules or infer that a TCP connection is established. A policy or
attachment change drains both directions before the new generation can admit.
State persistence must be atomic/durable and share the session owner's lock.
"""
import hashlib
import json
from copy import deepcopy
from ffn_fe100_nat import session_pair4
from ffn_fe100_sessions import key4, forwarding_entry4, uint
from ffn_fe100_flow_ids import assign_pair


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


class PolicyOwner:
    def __init__(self, sessions, load, save, bindings, qualified, *,
                 paths=None, nat_qualified=lambda: False, flow_ids=None):
        self.sessions,self.save,self.bindings,self.qualified=sessions,save,bindings,qualified
        # paths is a trusted, serialized next-hop owner readback, not a DP
        # observation or caller-supplied collection of hardware table indexes.
        self.paths,self.nat_qualified=paths,nat_qualified
        self.flow_ids=flow_ids
        self.dependencies={}
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
                if not stale:
                    for ident,request in list(self.dependencies.items()):
                        if ident not in self.sessions.sessions:
                            del self.dependencies[ident]
                        else:
                            self.directional_paths(request)
            except Exception:
                stale = True
        if stale or self.sessions.recovery_required:
            self.activated = False
            if (self.state['phase'] != 'blocked' or self.state['attachment'] is not None or
                    self.sessions.sessions or self.sessions.recovery_required):
                self.persist(phase='draining', attachment=None)
                self.sessions.recover()
                self.dependencies.clear()
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
        self.dependencies.clear()
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
        if self.flow_ids is None:raise RuntimeError('durable hardware flow-ID allocator is not commissioned')
        self.persist(phase='active',attachment=digest(self.bindings()))
        self.activated = True

    def admit(self, request):
        if isinstance(request,dict) and 'original' in request:
            return self.admit_pair(request)
        fields={'session_id','revision','policy_digest','rule_id','verdict','protocol','src','dst',
                'sport','dport','zone','ingress','egress','inspection_required','nat_required','established'}
        if not isinstance(request,dict) or set(request)!=fields:raise ValueError('invalid admission fields')
        uint(request['revision'],64,'policy revision')
        if not self.status()['admission_enabled']:raise RuntimeError('policy admission blocked')
        self.reconcile()
        if not self.activated or self.state['phase']!='active' or self.sessions.recovery_required:
            raise RuntimeError('policy admission blocked')
        bindings=self.bindings()
        if digest(bindings)!=self.state['attachment'] or self.qualified() is not True:
            self.activated=False;self.reconcile()
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
        entries=[forwarding_entry4(key,sid*2+i,hop,decrement_ttl=True)
                 for i,(key,hop) in enumerate(zip((k,reverse),hops))]
        entries=assign_pair(entries,self.flow_ids)
        self.sessions.install(sid,entries,self.state['revision'])
        self.reconcile()
        if not self.status()['admission_enabled']:
            raise RuntimeError('offload dependencies changed during installation; pair drained')
        return {'installed':True,'session_id':sid,'revision':self.state['revision'],'directions':2}

    def directional_paths(self, request):
        """Check route/neighbor/attachment leases for BOTH NAT directions.

        The path owner must return only commissioned, read-back next hops and
        ingress zones. Its revisions change before modifying those resources.
        This callback is intentionally absent from the production endpoint.
        """
        if self.paths is None:raise RuntimeError('directional path owner is not commissioned')
        snapshot=self.paths(deepcopy(request))
        if (not isinstance(snapshot,dict) or set(snapshot)!={'nat_digest','directions'} or
                snapshot['nat_digest']!=request['nat_digest'] or
                digest(snapshot)!=request['path_digest']):
            raise RuntimeError('NAT generation or directional path changed')
        directions=snapshot['directions']
        if not isinstance(directions,list) or len(directions)!=2:
            raise ValueError('two commissioned directional paths required')
        fields={'ingress','egress','destination','zone','next_hop',
                'route_revision','neighbor_revision','attachment_revision'}
        for path,ingress,egress,destination in zip(directions,
                (request['ingress'],request['egress']), (request['egress'],request['ingress']),
                (request['reply']['source'],request['original']['source'])):
            if (not isinstance(path,dict) or set(path)!=fields or
                    path['ingress']!=ingress or path['egress']!=egress or path['destination']!=destination):
                raise ValueError('directional path does not match the translated destination')
            uint(path['zone'],16,'ingress lookup zone');uint(path['next_hop'],16,'next-hop')
            for field in ('route_revision','neighbor_revision','attachment_revision'):
                self.sha256(path[field])
        if request['nat_required'] and self.nat_qualified() is not True:
            raise RuntimeError('production NAT offload is not qualified')
        return directions

    @staticmethod
    def sha256(value):
        if (not isinstance(value,str) or len(value)!=64 or
                any(c not in '0123456789abcdef' for c in value)):
            raise ValueError('SHA256 generation digest required')

    def admit_pair(self, request):
        """Admit an evaluator decision using kernel-selected original/reply.

        No address/port allocation or inference of a security verdict occurs.
        Legacy single-tuple admission remains non-NAT only. This path requires
        an independently commissioned path owner and NAT qualification.
        """
        fields={'session_id','revision','policy_digest','nat_digest','path_digest','rule_id','verdict',
                'original','reply','ingress','egress','inspection_required','nat_required','established'}
        if not isinstance(request,dict) or set(request)!=fields:raise ValueError('invalid paired admission fields')
        request=deepcopy(request)
        uint(request['revision'],64,'policy revision')
        for field in ('policy_digest','nat_digest','path_digest'):self.sha256(request[field])
        if not self.status()['admission_enabled']:raise RuntimeError('policy admission blocked')
        self.reconcile()
        if not self.status()['admission_enabled']:raise RuntimeError('policy admission blocked')
        if request['revision']!=self.state['revision'] or request['policy_digest']!=self.state['digest']:
            raise ValueError('stale policy decision')
        if (request['verdict']!='allow' or not isinstance(request['rule_id'],str) or
                not 1<=len(request['rule_id'])<=128 or request['inspection_required'] is not False or
                request['established'] is not True or type(request['nat_required']) is not bool):
            raise ValueError('flow requires software enforcement')
        ports=(request['ingress'],request['egress']);bindings=self.bindings()
        if any(type(p) is not int or str(p) not in bindings for p in ports) or ports[0]==ports[1]:
            raise ValueError('verified distinct front-port bindings required')
        for port in ports:
            if bindings[str(port)].get('enabled') is not True or bindings[str(port)].get('link') is not True:
                raise RuntimeError('packet offload attachment is unavailable')
        sid=uint(request['session_id'],31,'session ID')
        # Validate tuples before calling the path owner with their fields.
        session_pair4(sid,request['original'],request['reply'],0,[0,0])
        paths=self.directional_paths(request)
        entries=session_pair4(sid,request['original'],request['reply'],
                              [p['zone'] for p in paths],[p['next_hop'] for p in paths])
        translated=any(int.from_bytes(e[16:20],'big') & (3<<29) for e in entries)
        if translated!=request['nat_required']:raise ValueError('NAT decision does not match conntrack translation')
        entries=assign_pair(entries,self.flow_ids)
        self.sessions.install(sid,entries,self.state['revision'],lookup_zones=[p['zone'] for p in paths])
        self.dependencies[sid]=request
        # Hardware calls may take time. Withhold admission acknowledgement if
        # any dependency changed while the pair was being programmed.
        self.reconcile()
        if not self.status()['admission_enabled']:
            raise RuntimeError('offload dependencies changed during installation; pair drained')
        return {'installed':True,'session_id':sid,'revision':self.state['revision'],
                'directions':2,'nat':translated,'path_digest':request['path_digest']}

    def revoke(self, session_id):
        uint(session_id,31,'session ID')
        if session_id in self.sessions.sessions:self.sessions.remove(session_id)
        self.dependencies.pop(session_id,None)
        return {'removed':True,'session_id':session_id}

    def status(self):
        return self.state | {'sessions':len(self.sessions.sessions),
            'recovery_required':self.sessions.recovery_required,
            'admission_enabled':self.activated and self.state['phase']=='active' and not self.sessions.recovery_required}
