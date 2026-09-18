#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""MP-owned aggregate report; all hardware reads use fixed plane helpers."""
import asyncio
import json
from pathlib import Path
import sys
import time
sys.path.extend(['/opt/ffn-ngfw-v2','/opt/ffn-ngfw'])
from aggregate_config import plan,readiness


async def execute(action,payload,backend=None,directory=Path('/var/lib/ffn-ngfw/config')):
    if action!='status':
        from aggregate_activation import execute as activate
        return await asyncio.to_thread(activate,action,payload)
    if payload:raise ValueError('Status takes no fields')
    if backend is None:
        from hardware_backend import Controller
        backend=Controller()
    candidate=(directory/'candidate-config.xml').read_bytes();running=(directory/'running-config.xml').read_bytes()
    intent=plan(candidate);committed=plan(running)
    async def read(resource):
        value=await backend.run(resource,'status')
        return value,time.monotonic()
    observations=await asyncio.gather(*(read(resource) for resource in
        ('faceplate','network','lacp-observation')),return_exceptions=True)
    values=[v[0] if isinstance(v,tuple) and isinstance(v[0],dict) else {'error':'Observation unavailable'} for v in observations]
    if isinstance(observations[2],tuple):
        # CP inventory can take longer than an LACP fast timeout. Age the
        # sample while waiting, using MP monotonic time rather than DP wall time.
        elapsed=max(0,time.monotonic()-observations[2][1])
        values[2]['response_age_seconds']=elapsed
        for peer in values[2].get('ports',[]):
            peer['age_seconds']=peer.get('age_seconds',0)+elapsed
            peer['expired']=peer.get('expired',True) or peer['age_seconds']>=peer.get('timeout_seconds',0)
    committed_by_name={g['ae_name']:g for g in committed['aggregates']}
    from aggregate_activation import status as activation_status
    activation=activation_status()
    rows=[]
    for group in intent['aggregates']:
        row=dict(readiness(group,*values),committed=group==committed_by_name.get(group['ae_name']),
            running_revision=committed['revision'],activation_revision=activation['revision'],activation_supported=activation['activation_supported'],
            offload_ready=activation['offload_ready'],offload_blocker=activation['offload_blocker'])
        runtime=activation['groups'].get(group['ae_name'])
        row['activation']=runtime
        if activation['activation_supported']:
            row['blockers']=[b for b in row['blockers'] if b['code'] not in ('bcm-membership','lacp-negotiation','dataplane-attachment','dhcp-client','lldp')]
            if not runtime or not runtime.get('fresh'):
                row['state']='inactive'
                row['blockers'].append(dict(code='activation-inactive',message=(runtime or {}).get('error') or 'Committed aggregate owner is not active'))
        if runtime and runtime.get('fresh'):
            row['state']=runtime['state'];row['applied']=runtime.get('applied',False) and row['committed'] and runtime.get('running_revision')==committed['revision']
            dp=runtime.get('dataplane',{})
            row['runtime_members']=dp.get('members',{});row['distributing']=dp.get('distributing',[])
            peers=[]
            for member in row['members']:
                observed=dp.get('members',{}).get(str(member['port']),{})
                peer=observed.get('peer')
                member['partner_observation']=dict(actor=peer) if peer else None
                member['lacp_runtime']=observed
                if peer:peers.append(dict(port=member['port'],expired=False,actor=peer))
            row['partner_consistency']=readiness(group,values[0],values[1],dict(available=True,ports=peers))['partner_consistency']
            row['partner_consistency']['negotiated']=bool(row['distributing'])
            row['hardware_offload']=dp.get('hardware_offload',False)
            row['offload_scope']=dp.get('offload_scope')
            row['offload_tx']=dp.get('offload_tx',0)
            if runtime.get('control_only'):row['blockers'].append(dict(code='control-only',message='LACP qualification only; data collection, DHCP and routing are disabled'))
            elif dp.get('network',{}).get('enabled',True) or dp.get('network',{}).get('units'):row['blockers'].append(dict(code='transit-policy',message='Aggregate and VLAN transit defaults to deny until a security-policy binding is implemented'))
            if dp.get('network_error'):row['blockers'].append(dict(code='network-apply',message=dp['network_error']))
            if runtime.get('configuration_error'):row['blockers'].append(dict(code='network-configuration',message=runtime['configuration_error']))
            elif dp.get('network_update_pending'):row['blockers'].append(dict(code='network-pending',message='Parent networking is updating; LACP is retained'))
            if not row['distributing'] and not runtime.get('control_only'):row['blockers'].append(dict(code='negotiating',message='No members are distributing; inspect physical links and partner negotiation'))
        observed={u['name']:u for u in (runtime or {}).get('dataplane',{}).get('subinterfaces',[])}
        row['subinterfaces']=[]
        for unit in group.get('subinterfaces',[]):
            current=observed.get(unit['name'],{})
            ack=bool(runtime and runtime.get('configuration_revision') and runtime.get('configuration_revision')==runtime.get('dataplane',{}).get('configuration_revision'))
            ready=bool(unit.get('supported') and ack and runtime.get('fresh') and row['committed'] and runtime.get('running_revision')==committed['revision'] and current.get('applied') and current.get('tag')==unit['tag'])
            child=dict(unit,applied=ready,state='active' if ready else unit['state'],reason='Local VLAN attachment active; transit policy remains default-deny' if ready else unit['reason'],counters=current.get('counters',{}))
            row['subinterfaces'].append(child)
            if not ready:row['blockers'].append(dict(code='subinterface-attachment',message=unit['name']+': '+child['reason']))
        rows.append(row)
    if candidate!=(directory/'candidate-config.xml').read_bytes() or running!=(directory/'running-config.xml').read_bytes():
        raise ValueError('Configuration changed while reading aggregate status; refresh')
    return dict(owner='ffn-controld',provider='pa5200',revision=activation['revision'],
        candidate_revision=intent['revision'],running_revision=committed['revision'],aggregates=rows,
        orphan_members=intent['orphan_members'],running_aggregates=committed['aggregates'],
        observation=values[2],activation=activation,applied=bool(rows) and all(r['applied'] for r in rows))


if __name__=='__main__':
    try:print(json.dumps(asyncio.run(execute(sys.argv[1],json.load(sys.stdin)))))
    except ValueError as error:
        print(json.dumps({'error':str(error)}));raise SystemExit(2)
    except Exception as error:
        print(json.dumps({'error':str(error)}));raise SystemExit(1)
