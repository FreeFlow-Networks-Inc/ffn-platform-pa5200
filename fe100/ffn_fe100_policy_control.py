#!/usr/bin/env python3
"""CP policy barrier and recovery: no public flow-admission API."""
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,'/usr/local/lib/ffn')
from ffn_fe100_journal import Journal
from ffn_fe100_policy import PolicyOwner
from ffn_fe100_sessions import SessionManager

ROOT=Path('/var/lib/ffn/fe100')
ADMISSION_CONFIG=Path('/etc/ffn/fe100-admission.json')


def admission_mode(path=None,log=lambda line:None):
    """The operator's admission loop mode: {"mode": "off"|"observe"|"admit"}; absent or unreadable means observe."""
    from ffn_fe100_admission_loop import MODES
    path=ADMISSION_CONFIG if path is None else path
    try:
        value=json.loads(path.read_text())
        mode=value.get('mode') if isinstance(value,dict) else None
        if mode in MODES:return mode
        log('admission configuration %s names no valid mode; observing'%path)
    except FileNotFoundError:pass
    except (OSError,ValueError) as error:log('admission configuration unreadable (%s); observing'%str(error)[:120])
    return 'observe'


def paired_request(row,receiver,owner):
    """The paired admission request for one inventory row, as the dry-run evaluator builds it:
    the pair the DP's routes selected among the rule's, the applied bindings' indices, the owner's barrier."""
    from ffn_fe100_admission import request_for
    policy=receiver.policy if isinstance(receiver.policy,dict) else {}
    rule=row.get('rule') if isinstance(row.get('rule'),dict) else {}
    pairs=[list(p) for p in rule.get('interface_pairs') or () if isinstance(p,list) and len(p)==2]
    selected=(row.get('l3') or {}).get('pair') if isinstance(row.get('l3'),dict) else None
    if isinstance(selected,list) and list(selected) in pairs:names=tuple(selected)
    elif len(pairs)==1:names=tuple(pairs[0])
    else:raise ValueError('interface pair unresolved by the routes among %d authorised'%len(pairs))
    bindings=policy.get('bindings') or {};indices={}
    for name in names:
        binding=bindings.get(name)
        if not isinstance(binding,dict) or type(binding.get('index')) is not int:
            raise ValueError('interface owner is absent from applied bindings: '+name)
        indices[name]=binding['index']
    return request_for(row,policy,owner,indices,names)


class PolicyController:
    """One journal owner for the lifetime of the supervised CP process."""
    def __init__(self):
        ROOT.mkdir(parents=True,exist_ok=True)
        self.journal=journal=Journal(ROOT/'policy-sessions.sqlite3')
        try:
            journal.db.execute('CREATE TABLE IF NOT EXISTS policy_state (id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
            journal.db.commit()
            def load():
                row=journal.db.execute('SELECT body FROM policy_state WHERE id=1').fetchone()
                return json.loads(row[0]) if row else None
            def save(state):
                with journal.db:
                    journal.db.execute('INSERT OR REPLACE INTO policy_state VALUES (1,?)',(json.dumps(state),))
            # Packet qualification is audited per hardware lifetime (CP boot,
            # BCM owner) and lifts the front-port and NAT gates; activation
            # stays a separate explicit operation.
            from ffn_fe100_qualification import Qualification
            self.qualification=Qualification()
            class Backend:
                def __init__(self):self.native=None
                def endpoint(self):
                    if self.native is None:
                        from ffn_fe100_live_sessions import LiveSessions
                        from ffn_fe100_session_adapter import NativeSessionAdapter
                        self.live=LiveSessions(True)
                        self.native=NativeSessionAdapter(self.live,lambda:dict({'summary':{},'physical_transport_verified':False},
                                                                                **self.qualification.health()))
                    return self.native
                def fetch(self,key):return self.endpoint().fetch(key)
                def delete(self,key):return self.endpoint().delete(key)
                def readiness(self):return ['general production admission is not qualified']
            self.owner=PolicyOwner(SessionManager(Backend(),journal),load,save,lambda:{},self.qualification.front_port,
                                   nat_qualified=self.qualification.nat)
            # The flow-ID namespace is commissioned per hardware generation
            # (CP boot) once the FE100 reads initialised and the range is
            # proved on it; until then the owner has no allocator.
            from ffn_fe100_flow_namespace import FlowNamespace
            boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            self.namespace=FlowNamespace(journal.db,boot,log=lambda line:print(line,flush=True))
            from ffn_fe100_observations import Observations
            def withdraw():
                self.owner.activated=False
                self.owner.reconcile()
            self.observations=Observations(withdraw,configuration_digest=lambda:self.owner.state['digest'])
            # The admission loop drives the owners above from the held inventory
            # (fe100/ADMISSION-LOOP.md). `admit` needs a commissioned directional
            # path owner behind admit_pair; without one it observes, and says so.
            from ffn_fe100_admission_loop import AdmissionLoop
            log=lambda line:print(line,flush=True)
            mode=admission_mode(log=log);lifecycle=None;request=None
            if mode=='admit':
                if self.owner.paths is None:
                    log('admission mode admit requested but no directional path owner is commissioned; observing');mode='observe'
                else:
                    from ffn_fe100_lifecycle import SessionLifecycle
                    from ffn_fe100_policy import digest as generation_digest
                    def generation():
                        receiver=self.observations.receiver;policy=getattr(receiver,'policy',None) or {}
                        return generation_digest([self.owner.state['digest'],self.owner.state['attachment'],
                                                  policy.get('nat_digest'),(policy.get('l3') or {}).get('snapshot_digest')])
                    lifecycle=SessionLifecycle(self.owner,generation=generation);request=paired_request
            self.admission_loop=AdmissionLoop(mode,lifecycle=lifecycle,request=request,log=log)
        except BaseException:
            journal.close();raise

    def execute(self,operation,payload):
        if isinstance(operation,str) and operation.startswith('observe-'):
            if not isinstance(payload,dict):raise ValueError('observation payload must be an object')
            if operation=='observe-start':
                if set(payload)!={'nonce'}:raise ValueError('invalid observation start')
                return self.observations.start(payload['nonce'])
            if payload.get('control_owner')!=os.environ.get('FFN_FE100_GUARD_NONCE') or not payload.get('control_owner'):
                raise ValueError('observation control owner changed')
            body={k:v for k,v in payload.items() if k!='control_owner'}
            if operation=='observe-chunk':return self.observations.chunk(body)
            if operation=='observe-close' and set(body)=={'nonce'}:return self.observations.close(body['nonce'])
            raise ValueError('unsupported observation operation')
        if operation not in ('status','replace','reconcile','qualify'):raise ValueError('unsupported policy operation')
        if not isinstance(payload,dict):raise ValueError('policy payload must be an object')
        if ((operation in ('status','reconcile') and payload) or (operation=='replace' and set(payload)!={'revision','digest'})
                or (operation=='qualify' and set(payload)!={'scope','summary'})):raise ValueError('invalid policy barrier fields')
        self.observations.tick()
        try:self.owner.flow_ids=self.namespace.ensure(self.owner.flow_ids)
        except Exception as error:self.namespace.reason='flow-ID namespace: '+str(error)[:200]
        if operation in ('status','reconcile'):
            # One bounded pass per tick over the inventory the service holds.
            try:self.admission_loop.run(self.observations.receiver,self.owner.status())
            except Exception as error:print('admission loop pass failed: '+str(error)[:200],flush=True)
        if operation=='replace':
            result=self.owner.replace(payload['revision'],payload['digest'])
            self.observations.fence('configuration replacement requires fresh DP snapshot')
            self.observations.receiver=None
        elif operation=='reconcile':result=self.owner.reconcile()
        elif operation=='qualify':
            # The audited packet result for this hardware lifetime; the owner's
            # gates read it afresh on every reconcile and admission.
            qualification=self.qualification.record(payload['summary'],payload['scope'])
            result=dict(self.owner.reconcile(),qualification=qualification)
        else:result=self.owner.status()
        result=dict(result,observations=self.observations.status())
        if operation!='status':return result
        from ffn_fe100_nat import capabilities
        from ffn_fe100_attachment_runtime import resolve
        from ffn_fe100_admission import MODE,evaluate
        try:attachments=resolve(self.observations.configuration,self.observations.receiver,self.owner.state['digest'])
        except Exception as error:
            attachments=dict(available=False,hardware_admission=False,interfaces=[],reason=str(error)[:256])
        # Dry run of the production gate chain over the held inventory: it
        # reports what blocks each session and installs nothing.
        intent=self.observations.configuration
        capability=dict(capabilities(),nat_packet_qualification=self.qualification.nat())
        try:admission=evaluate(self.observations.receiver,attachments,result,capability,
                               flow_ids=self.owner.flow_ids is not None,qualified=self.owner.qualified(),
                               configuration_digest=intent['config_digest'] if isinstance(intent,dict) else None)
        except Exception as error:
            admission=dict(mode=MODE,hardware_admission=False,installed=0,available=False,reason=str(error)[:256])
        return dict(result,capabilities=capability,attachments=attachments,admission=admission,
                    admission_loop=self.admission_loop.status(),
                    flow_ids=self.namespace.status(self.owner.flow_ids),qualification=self.qualification.current())

    def close(self):self.journal.close()


def control(operation,payload):
    owner=PolicyController()
    try:return owner.execute(operation,payload)
    finally:owner.close()


def dispatch(operation,payload):
    if (ROOT/'control-service-required').exists():
        from ffn_fe100_control_socket import request
        return request(operation,payload)
    return control(operation,payload)


if __name__=='__main__':
    operation=sys.argv[1] if len(sys.argv)==2 else 'status'
    data=sys.stdin.buffer.read(65537)
    if len(data)>65536:raise ValueError('request too large')
    payload=json.loads(data) if data.strip() else {}
    print(json.dumps(dispatch(operation,payload)))
