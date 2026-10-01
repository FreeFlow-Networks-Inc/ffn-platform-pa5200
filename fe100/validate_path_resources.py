"""Table-only validation of durable FE100 path resources on commissioned CP.

Uses the already reserved lab slots 30/31, requires empty hardware flow tables,
never changes ports/LIF/parser/BCM/customer policy, and sends no packets.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
from ffn_fe100_journal import Journal
from ffn_fe100_path_owner import PathOwner
from ffn_fe100_policy import digest
from ffn_fe100_resource_tables import ResourceTables
from ffn_fe100_packet_lab import Lab,ROOT

POOLS={'smac':[30,31],'nexthop':[30,31]}
KEY=digest('isolated FE100 resource owner qualification')


def fixture():
    directions=[]
    for i,(a,b,ip,zone,lif) in enumerate([(5,13,'198.51.100.10',4094,31),(13,5,'192.0.2.10',4093,30)]):
        directions.append(dict(ingress=a,egress=b,destination=ip,zone=zone,egress_lif=lif,
            source_mac='02:52:20:ab:cd:01',destination_mac='02:52:20:ab:cd:02',vlan=None,mtu=1518,
            route_revision=digest(['lab route',i]),neighbor_revision=digest(['lab neighbor',i]),
            attachment_revision=digest(['lab attachment',i])))
    return dict(nat_digest=digest('isolated fixture NAT'),directions=directions)


def run():
    lab=Lab();backend=None;journal=None;owner=None;bridge=None;flow_journal=None
    report=dict(schema=1,stage='started',scope='isolated-resource-tables',production_admission=False,
                packets_sent=0,tests=[],cleanup_verified=False)
    output=ROOT/('resource-validation-'+str(time.time_ns())+'.json')
    def save():
        temp=output.with_suffix('.new')
        with temp.open('w') as f:
            os.chmod(temp,0o600);json.dump(report,f,indent=2);f.flush();os.fsync(f.fileno())
        temp.replace(output)
    def empty_flows(key):
        state=lab.call('readiness')
        if state['commissioning_blockers']:raise RuntimeError('Hardware readiness changed')
        return all(state['registers'][r]==0 for r in ('0x40428','0x40450'))
    try:
        save();health=lab.call('readiness');report['cp_boot_id']=health['cp_boot_id']
        if not empty_flows(KEY):raise RuntimeError('Resource validation requires empty hardware flow tables')
        backend=ResourceTables(POOLS,lock_fd=lab.lock.fileno())
        journal=Journal(ROOT/'resource-validation.sqlite3');plan=fixture();allow=[True]
        def make():return PathOwner(journal.db,backend,POOLS,health['cp_boot_id'],lambda k:copy.deepcopy(plan),lambda k:allow[0] and empty_flows(k))
        owner=make();owner.recover()
        for kind,indices in POOLS.items():
            if any(backend.fetch(kind,i) is not None for i in indices):raise RuntimeError('Reserved lab resource occupied')
        snap=owner.acquire(KEY,plan)
        if owner.snapshot(KEY)!=snap:raise RuntimeError('Resource readback changed')
        owner.release(KEY);report['tests'].append('paired-source-mac-and-next-hop-readback-removal');save()
        owner.acquire(KEY,plan);plan['directions'][0]['neighbor_revision']=digest('changed neighbor')
        owner.reconcile()
        if owner.paths:raise RuntimeError('Stale path survived neighbor change')
        report['tests'].append('neighbor-generation-drain');save()
        owner.acquire(KEY,plan);allow[0]=False
        try:owner.release(KEY)
        except RuntimeError:
            if len(owner.paths)!=1:raise RuntimeError('Unacknowledged drain lost ownership')
        else:raise RuntimeError('Unacknowledged drain accepted')
        allow[0]=True;owner.recover();report['tests'].append('drain-ack-required-before-reclaim');save()
        owner.acquire(KEY,plan);owner=make()
        if not owner.recovery_required:raise RuntimeError('Restart adopted a live path')
        owner.recover();report['tests'].append('durable-restart-recovery');save()
        original_insert=backend.insert
        def interrupted(kind,index,data):
            original_insert(kind,index,data)
            if kind=='nexthop':raise TimeoutError('simulated lost write acknowledgement')
        backend.insert=interrupted
        try:owner.acquire(KEY,plan)
        except TimeoutError:pass
        else:raise RuntimeError('Interrupted write was not simulated')
        finally:backend.insert=original_insert
        owner=make();owner.recover();report['tests'].append('ambiguous-native-write-recovery');save()
        # Couple the real resource owner to the previously qualified paired
        # NAT table adapter. Fixture ports/zones remain isolated; no packets.
        from ffn_fe100_live_sessions import LiveSessions
        from ffn_fe100_sessions import SessionManager
        from ffn_fe100_nat import session_pair4
        from ffn_fe100_policy import PolicyOwner
        from ffn_fe100_path_sessions import PathSessions
        from ffn_fe100_lifecycle import SessionLifecycle
        from validate_nat_lifecycle import LabBackend,fixture as session_fixture
        request,_=session_fixture();request.pop('path_digest')
        request['nat_digest']=plan['nat_digest']
        plan['directions'][0]['destination']=request['reply']['source']
        plan['directions'][1]['destination']=request['original']['source']
        entries=session_pair4(request['session_id'],request['original'],request['reply'],[4094,4093],[30,31])
        io=LiveSessions(True,lock_fd=lab.lock.fileno(),commissioning=True)
        flow_journal=Journal(ROOT/'resource-flow-validation.sqlite3')
        flow_backend=LabBackend(io,entries);manager=SessionManager(flow_backend,flow_journal);manager.recover()
        policy=PolicyOwner(manager,lambda:None,lambda s:None,
            lambda:{'5':dict(enabled=True,link=True),'13':dict(enabled=True,link=True)},
            lambda:not flow_backend.readiness(),nat_qualified=lambda:True)
        bridge=PathSessions(policy,owner)
        policy.replace(0,request['policy_digest']);policy.activate(1,request['policy_digest'])
        bridge.admit(request,plan)
        if any(flow_backend.fetch(e[:16])!=e for e in entries):raise RuntimeError('Resource-backed NAT readback mismatch')
        plan['directions'][1]['neighbor_revision']=digest('integrated neighbor change')
        bridge.reconcile()
        if owner.paths or manager.sessions or any(flow_backend.fetch(e[:16]) is not None for e in entries):
            raise RuntimeError('Resource-backed NAT drain not acknowledged')
        report['tests'].append('paired-NAT-flow-and-next-hop-ordered-drain');save()
        # Fixed lab keys only; the live observation feed never admits flows.
        generation=[digest('lab applied topology')];now=[time.monotonic()]
        producer=dict(boot_id=health['cp_boot_id'],pid=os.getpid(),
            process_start=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19],
            stream_id=str(uuid.uuid4()))
        delete=backend.delete
        def ordered_delete(kind,index):
            if manager.sessions or any(flow_backend.fetch(e[:16]) is not None for e in entries):
                raise RuntimeError('Resource deletion preceded paired flow acknowledgement')
            delete(kind,index)
        backend.delete=ordered_delete
        for trigger in ('close','idle-expiry','heartbeat-expiry','topology-generation',
                        'neighbor-generation','producer-restart'):
            life=SessionLifecycle(policy,paths=bridge,generation=lambda:generation[0],
                clock=lambda:now[0],idle_timeout=4,heartbeat_timeout=10)
            life.start(producer,1,request['policy_digest'])
            life.event(producer,1,'open',request)
            if len(owner.paths)!=1 or any(flow_backend.fetch(e[:16])!=e for e in entries):
                raise RuntimeError('Leased resource-backed NAT readback mismatch')
            if trigger=='close':life.event(producer,2,'close',{'session_id':request['session_id']})
            elif trigger in ('idle-expiry','heartbeat-expiry'):
                now[0]+=4 if trigger=='idle-expiry' else 10;life.tick()
            elif trigger=='topology-generation':generation[0]=digest('changed lab topology');life.tick()
            elif trigger=='neighbor-generation':
                plan['directions'][0]['neighbor_revision']=digest('leased neighbor replacement');life.tick()
            else:
                try:life.event(producer|{'stream_id':str(uuid.uuid4())},2,'heartbeat',{})
                except RuntimeError:
                    if life.status()['synchronized']:raise
                else:raise RuntimeError('Restarted lease producer accepted')
            if (owner.paths or manager.sessions or any(flow_backend.fetch(e[:16]) is not None for e in entries)
                    or any(backend.fetch(k,i) is not None for k,ids in POOLS.items() for i in ids)):
                raise RuntimeError('Leased flow/resource withdrawal not acknowledged')
            report['tests'].append('leased-ordered-withdrawal-'+trigger);save()
        report['stage']='completed'
    except BaseException as error:report.update(stage='failed',error=str(error));raise
    finally:
        try:
            if owner is not None:
                allow[0]=True
                if bridge is not None:bridge.recover()
                else:owner.recover()
                report['cleanup_verified']=not owner.paths and all(backend.fetch(k,i) is None for k,ids in POOLS.items() for i in ids)
                if not report['cleanup_verified']:raise RuntimeError('Resource cleanup not confirmed')
            report['after_flows_empty']=empty_flows(KEY)
        except BaseException as error:report.update(stage='cleanup-required',cleanup_error=str(error));raise
        finally:
            save()
            if journal is not None:journal.close()
            if flow_journal is not None:flow_journal.close()
            if backend is not None:backend.close()
            for p,err in lab.workers.values():
                try:p.stdin.close()
                except BrokenPipeError:pass
                try:p.wait(timeout=2)
                except subprocess.TimeoutExpired:p.kill();p.wait()
                err.close()
            lab.lock.close()
            print(json.dumps(dict(report,journal=str(output))))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--run',action='store_true',required=True);parser.parse_args();run()
