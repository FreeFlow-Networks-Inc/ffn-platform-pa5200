#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""MP-owned aggregate report; all hardware reads use fixed plane helpers."""
import asyncio
import json
from pathlib import Path
import sys
import time
from aggregate_config import plan,readiness


async def execute(action,payload,backend=None,directory=Path('/var/lib/ffn-ngfw/config')):
    if action!='status' or payload:raise ValueError('Aggregate hardware activation is not commissioned; only status is available')
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
    rows=[]
    for group in intent['aggregates']:
        rows.append(dict(readiness(group,*values),committed=group==committed_by_name.get(group['ae_name'])))
    if candidate!=(directory/'candidate-config.xml').read_bytes() or running!=(directory/'running-config.xml').read_bytes():
        raise ValueError('Configuration changed while reading aggregate status; refresh')
    return dict(owner='ffn-controld',provider='pa5200',revision=int(intent['revision'][:12],16),
        candidate_revision=intent['revision'],running_revision=committed['revision'],aggregates=rows,
        orphan_members=intent['orphan_members'],running_aggregates=committed['aggregates'],
        observation=values[2],applied=False)


if __name__=='__main__':
    try:print(json.dumps(asyncio.run(execute(sys.argv[1],json.load(sys.stdin)))))
    except Exception as error:
        print(json.dumps({'error':str(error)}));raise SystemExit(2)
