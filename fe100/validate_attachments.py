"""Isolated LIF/LEF ownership test; sends no packets and changes no port state.

Requires two explicitly selected, administratively disabled optical ports,
empty flow tables and reserved lab LIF28/29, LEF30/31. No BCM redirect, parser,
QMAP, next-hop or policy changes. Table readback does not qualify forwarding.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import time
from ffn_fe100_attachments import AttachmentOwner,NativeEncoder
from ffn_fe100_journal import Journal
from ffn_fe100_lab_ports import profile
from ffn_fe100_packet_lab import Lab,ROOT
from ffn_fe100_policy import digest
from ffn_fe100_resource_tables import ResourceTables


def run(ports):
    from ffn_faceplate import call
    board=profile(ports)
    def disabled():
        rows={p['port']:p for p in call({'op':'port.list'})['ports']}
        return all(rows[p]['enabled'] is False for p in board['physical'].values())
    if not disabled():raise RuntimeError('Both isolated front ports must remain administratively disabled')
    lab=Lab();backend=None;journal=None;owner=None;allow=[True]
    report=dict(schema=1,scope='isolated-attachment-tables',production_admission=False,
        stage='started',packets_sent=0,tests=[],cleanup_verified=False)
    output=ROOT/('attachment-validation-'+str(time.time_ns())+'.json')
    def save():
        temp=output.with_suffix('.new')
        with temp.open('w') as f:
            os.chmod(temp,0o600);json.dump(report,f,indent=2);f.flush();os.fsync(f.fileno())
        temp.replace(output)
    def empty():
        health=lab.call('readiness')
        if health['commissioning_blockers']:raise RuntimeError('Hardware readiness changed')
        return all(health['registers'][r]==0 for r in ('0x40428','0x40450'))
    try:
        save()
        if not empty():raise RuntimeError('Empty hardware flow tables required')
        health=lab.call('readiness')
        pools=dict(lif=[28,29],lef=[30,31])
        backend=ResourceTables(pools,lock_fd=lab.lock.fileno())
        journal=Journal(ROOT/'attachment-validation.sqlite3');encoder=NativeEncoder()
        plan=dict(interface='isolated-attachment',binding_revision=digest(['lab',board]),
            ports=sorted(ports),vlan=4000,zone=4094,miss_next_hop=29)
        events=[]
        def withdraw(name):events.append('withdraw');return disabled()
        def drain(name):events.append('drain');return allow[0] and empty()
        def make():return AttachmentOwner(journal.db,backend,pools,health['cp_boot_id'],
            lambda name:copy.deepcopy(plan),withdraw,drain,encoder)
        owner=make();owner.recover()
        if any(backend.fetch(k,i) is not None for k,indices in pools.items() for i in indices):
            raise RuntimeError('Reserved lab attachment slot occupied')
        result=owner.acquire(plan['interface'],plan)
        if result['hardware_admission'] is not False or len(result['ingress_lifs'])!=2:
            raise RuntimeError('Incorrect attachment acknowledgement')
        owner.release(plan['interface'])
        report['tests'].append('tagged-member-LIF-LEF-readback-removal');save()
        plan['vlan']=0
        owner.acquire(plan['interface'],plan);owner.release(plan['interface'])
        report['tests'].append('exact-untagged-member-readback-removal');save()
        owner.acquire(plan['interface'],plan);plan['binding_revision']=digest('new lab binding')
        owner.reconcile()
        if owner.rows:raise RuntimeError('Stale attachment survived binding change')
        report['tests'].append('binding-generation-withdrawal');save()
        owner.acquire(plan['interface'],plan);allow[0]=False
        try:owner.release(plan['interface'])
        except RuntimeError:
            if len(owner.rows)!=1:raise RuntimeError('Unacknowledged drain lost journal')
        else:raise RuntimeError('Unacknowledged drain accepted')
        allow[0]=True;owner.recover()
        report['tests'].append('dependent-flow-drain-required');save()
        owner.acquire(plan['interface'],plan);owner=make()
        if not owner.recovery_required:raise RuntimeError('Restart adopted attachment')
        owner.recover();report['tests'].append('durable-restart-withdrawal');save()
        insert=backend.insert
        def lost(kind,index,raw):
            insert(kind,index,raw)
            if kind=='lif':raise TimeoutError('simulated lost native acknowledgement')
        backend.insert=lost
        try:owner.acquire(plan['interface'],plan)
        except TimeoutError:pass
        else:raise RuntimeError('Lost acknowledgement was not simulated')
        finally:backend.insert=insert
        owner=make();owner.recover();report['tests'].append('ambiguous-LIF-write-recovery');save()
        if len(events)%2 or any(events[i:i+2]!=['withdraw','drain'] for i in range(0,len(events),2)):
            raise RuntimeError('Ingress withdrawal did not precede dependent flow drain')
        report['tests'].append('withdraw-before-drain-order');report['stage']='completed'
    except BaseException as error:report.update(stage='failed',error=str(error));raise
    finally:
        try:
            allow[0]=True
            if owner is not None:
                owner.recover()
                report['cleanup_verified']=not owner.rows and all(backend.fetch(k,i) is None for k,indices in pools.items() for i in indices)
                if not report['cleanup_verified']:raise RuntimeError('Attachment cleanup not verified')
            report['after_flows_empty']=empty();report['ports_still_disabled']=disabled()
        except BaseException as error:report.update(stage='cleanup-required',cleanup_error=str(error));raise
        finally:
            save()
            if journal is not None:journal.close()
            if backend is not None:backend.close()
            lab.close()
            print(json.dumps(dict(report,journal=str(output))))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',action='store_true',required=True)
    p.add_argument('--ports',required=True,help='two disabled isolated optical ports, comma separated')
    args=p.parse_args();run([int(v) for v in args.ports.split(',')])
