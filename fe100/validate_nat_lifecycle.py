#!/usr/bin/env python3
"""Isolated FE100 paired NAT table/lifecycle validation, without packet I/O.

Only fixed benchmark tuples in reserved zones 4093/4094 are writable. No
parser, port, next-hop, route or customer policy is programmed. The lab backend
does not alter NativeSessionAdapter's production qualification requirements.
Rerunning recovers the exact durable lab journal before starting a new test.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import time
import uuid

from ffn_fe100_journal import Journal
from ffn_fe100_lifecycle import SessionLifecycle
from ffn_fe100_nat import session_pair4
from ffn_fe100_policy import PolicyOwner, digest
from ffn_fe100_session_adapter import encode_native, decode_native
from ffn_fe100_sessions import SessionManager, entry4, owned_variants


def fixture():
    original=dict(source='198.18.0.1',destination='198.18.0.2',source_port=49101,destination_port=49102,protocol=6)
    reply=dict(source='198.18.0.2',destination='198.18.0.3',source_port=49102,destination_port=49103,protocol=6)
    snapshot=dict(nat_digest=digest('isolated NAT table test'),directions=[
        dict(ingress=5,egress=13,destination=reply['source'],zone=4094,next_hop=31,
             route_revision=digest('lab route'),neighbor_revision=digest('lab neighbor'),attachment_revision=digest('lab attachment')),
        dict(ingress=13,egress=5,destination=original['source'],zone=4093,next_hop=30,
             route_revision=digest('lab reverse route'),neighbor_revision=digest('lab reverse neighbor'),attachment_revision=digest('lab attachment'))])
    request=dict(session_id=2048,revision=1,policy_digest=digest('isolated security decision'),
                 nat_digest=snapshot['nat_digest'],path_digest=digest(snapshot),
                 rule_id='isolated-table-validation',verdict='allow',original=original,reply=reply,
                 ingress=5,egress=13,inspection_required=False,nat_required=True,established=True)
    return request,snapshot


class LabBackend:
    def __init__(self,io,entries):
        self.io=io;self.entries={e[:16]:e for e in entries}

    def readiness(self):return self.io.status()['commissioning_blockers']

    def reserve_pair(self):
        # These table-only validators send no packets and use an empty ASIC.
        # Keep their fixed, scoped identities; never use this backend as the
        # production counter namespace allocator.
        return tuple(int.from_bytes(e[36:40],'big') for e in self.entries.values())

    def call(self,operation,wire):
        expected=self.entries.get(wire[:16])
        if expected is None or wire not in owned_variants(expected):
            raise ValueError('outside fixed paired NAT lab scope')
        rc,native=self.io.call(operation,encode_native(wire))
        if rc not in ((0,3) if operation=='fetch' else (0,)):
            raise RuntimeError('lab session '+operation+' failed: '+str(rc))
        return rc,native

    def fetch(self,key):
        expected=self.entries.get(key)
        if expected is None:raise ValueError('outside fixed paired NAT lab keys')
        rc,native=self.call('fetch',expected)
        return None if rc==3 else decode_native(native,key)

    def insert(self,wire):
        reasons=self.readiness()
        if reasons:raise RuntimeError('; '.join(reasons))
        self.call('insert',entry4(wire[:16],int.from_bytes(wire[36:40],'big')))
        self.call('update',wire)

    def delete(self,key):
        actual=self.fetch(key)
        if actual is not None:self.call('delete',actual)


def validate(io,root):
    request,snapshot=fixture();baseline=copy.deepcopy(snapshot)
    entries=session_pair4(2048,request['original'],request['reply'],[4094,4093],[31,30])
    journal=Journal(root/'nat-lifecycle-lab.sqlite3')
    report=dict(schema=1,scope='isolated-session-table-lifecycle',cp_boot_id=io.status()['cp_boot_id'],
                production_admission=False,external_wire_verified=False,packets_sent=0,
                tests=[],cleanup_verified=False,stage='started')
    path=root/('nat-lifecycle-validation-'+str(time.time_ns())+'.json')
    def save():
        temp=path.with_suffix('.new')
        with temp.open('w') as f:
            os.chmod(temp,0o600);json.dump(report,f,indent=2);f.flush();os.fsync(f.fileno())
        os.replace(temp,path)
    backend=LabBackend(io,entries);manager=SessionManager(backend,journal);owner=None
    try:
        save();manager.recover()
        before=io.status();report['before']=before;save()
        if before['commissioning_blockers'] or any(before['registers'][r] for r in ('0x40428','0x40450')):
            raise RuntimeError('paired lab requires healthy, empty FE100 session tables')
        owner=PolicyOwner(manager,lambda:None,lambda state:None,
            lambda:{'5':dict(enabled=True,link=True),'13':dict(enabled=True,link=True)},
            lambda:not backend.readiness(),paths=lambda r:copy.deepcopy(snapshot),nat_qualified=lambda:True,
            flow_ids=backend)
        # These fixture paths authorize this table-only lab, never packet
        # forwarding. A production path owner must commission real resources.
        owner.replace(0,request['policy_digest'])
        for reason in ('close','lease-expiry','NAT-generation','neighbor-generation','producer-restart'):
            snapshot.clear();snapshot.update(copy.deepcopy(baseline));now=[time.monotonic()]
            producer=dict(boot_id=before['cp_boot_id'],pid=os.getpid(),
                process_start=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19],stream_id=str(uuid.uuid4()))
            life=SessionLifecycle(owner,clock=lambda:now[0],idle_timeout=4)
            life.start(producer,1,request['policy_digest'])
            life.event(producer,1,'open',request)
            if any(backend.fetch(e[:16])!=e for e in entries):raise RuntimeError('paired NAT readback mismatch')
            if reason=='close':life.event(producer,2,'close',{'session_id':2048})
            elif reason=='lease-expiry':now[0]+=4;life.tick()
            elif reason=='NAT-generation':snapshot['nat_digest']=digest('changed NAT');life.tick()
            elif reason=='neighbor-generation':
                snapshot['directions'][1]['neighbor_revision']=digest('changed neighbor');life.tick()
            else:
                try:life.event(producer | {'stream_id':str(uuid.uuid4())},2,'heartbeat',{})
                except RuntimeError:
                    if life.status()['synchronized']:raise
                else:raise RuntimeError('restarted producer was accepted')
            if manager.sessions or any(backend.fetch(e[:16]) is not None for e in entries):
                raise RuntimeError('paired removal was not acknowledged')
            report['tests'].append(dict(trigger=reason,paired_nat_readback=True,paired_removal=True));save()
        report['stage']='completed'
    except BaseException as error:
        report.update(stage='failed',error=str(error));raise
    finally:
        try:
            if owner is not None:owner.activated=False
            manager.recover()
            report['cleanup_verified']=not manager.sessions
            report['after']=io.status()
        except Exception as error:
            report.update(stage='cleanup-required',cleanup_error=str(error))
            raise
        finally:save();journal.close();print(json.dumps(dict(report,journal=str(path))))
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true',required=True)
    parser.parse_args()
    from ffn_fe100_live_sessions import LiveSessions,ROOT
    validate(LiveSessions(True,commissioning=True),ROOT)
