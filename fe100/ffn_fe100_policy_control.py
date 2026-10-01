#!/usr/bin/env python3
"""CP policy barrier and recovery: no public flow-admission API."""
import json
from pathlib import Path
import sys
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
        except BaseException:
            journal.close();raise

    def execute(self,operation,payload):
        if operation not in ('status','replace','reconcile'):raise ValueError('unsupported policy operation')
        if not isinstance(payload,dict):raise ValueError('policy payload must be an object')
        if (operation in ('status','reconcile') and payload) or (operation=='replace' and
                set(payload)!={'revision','digest'}):raise ValueError('invalid policy barrier fields')
        if operation=='replace':return self.owner.replace(payload['revision'],payload['digest'])
        if operation=='reconcile':return self.owner.reconcile()
        from ffn_fe100_nat import capabilities
        return dict(self.owner.status(),capabilities=capabilities())

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
