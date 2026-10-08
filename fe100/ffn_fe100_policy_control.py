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
            class Backend:
                def __init__(self):self.native=None
                def endpoint(self):
                    if self.native is None:
                        from ffn_fe100_live_sessions import LiveSessions
                        from ffn_fe100_session_adapter import NativeSessionAdapter
                        self.live=LiveSessions(True)
                        self.native=NativeSessionAdapter(self.live,lambda:{'summary':{},'physical_transport_verified':False})
                    return self.native
                def fetch(self,key):return self.endpoint().fetch(key)
                def delete(self,key):return self.endpoint().delete(key)
                def readiness(self):return ['general production admission is not qualified']
            self.owner=PolicyOwner(SessionManager(Backend(),journal),load,save,lambda:{},lambda:False)
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
        if operation not in ('status','replace','reconcile'):raise ValueError('unsupported policy operation')
        if not isinstance(payload,dict):raise ValueError('policy payload must be an object')
        if (operation in ('status','reconcile') and payload) or (operation=='replace' and
                set(payload)!={'revision','digest'}):raise ValueError('invalid policy barrier fields')
        self.observations.tick()
        try:self.owner.flow_ids=self.namespace.ensure(self.owner.flow_ids)
        except Exception as error:self.namespace.reason='flow-ID namespace: '+str(error)[:200]
        if operation=='replace':
            result=self.owner.replace(payload['revision'],payload['digest'])
            self.observations.fence('configuration replacement requires fresh DP snapshot')
            self.observations.receiver=None
        elif operation=='reconcile':result=self.owner.reconcile()
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
        try:admission=evaluate(self.observations.receiver,attachments,result,capabilities(),
                               flow_ids=self.owner.flow_ids is not None,qualified=self.owner.qualified(),
                               configuration_digest=intent['config_digest'] if isinstance(intent,dict) else None)
        except Exception as error:
            admission=dict(mode=MODE,hardware_admission=False,installed=0,available=False,reason=str(error)[:256])
        return dict(result,capabilities=capabilities(),attachments=attachments,admission=admission,
                    flow_ids=self.namespace.status(self.owner.flow_ids))

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
