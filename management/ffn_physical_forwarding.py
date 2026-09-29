#!/usr/bin/env python3
"""Journaled CP redirect for one physical interface; never requires carrier."""
import json
from pathlib import Path
import sys
import uuid
import ffn_wan_forwarding as wan
from ffn_copper_forwarding import epoch
from ffn_faceplate import call
from ffn_aggregate_hardware import PORTS, FACEPLATE_LOCK, acquire, STATE as AGGREGATES
from ffn_packet_fabric import COPPER_PORTS, ensure

MAPPING=dict(PORTS) | COPPER_PORTS


def hardware(port,mode):
    # The same bounded SDK recipe/readback used by WAN1, selecting a physical
    # port solely from this hardware family's commissioned board map.
    recipe=wan.RECIPE;wan.RECIPE=recipe.replace('28',str(MAPPING[port]))
    try:return wan.hardware(mode)
    finally:wan.RECIPE=recipe


def execute(action,payload):
    if not isinstance(payload,dict) or type(payload.get('port')) is not int or payload['port'] not in MAPPING or payload['port']==1:
        raise ValueError('Independent physical port required; port 1 retains its WAN owner')
    port=payload['port'];fields={'port'} if action=='status' else {'port','epoch','dp_boot_id'}
    if action not in ('status','start','stop') or set(payload)!=fields:raise ValueError('Invalid physical operation')
    if action!='status' and str(uuid.UUID(payload['dp_boot_id']))!=payload['dp_boot_id']:raise ValueError('DP boot identity required')
    path=Path('/etc/ffn/physical-'+str(port)+'.json')
    with Path('/run/ffn-physical-'+str(port)+'.lock').open('a') as lock,FACEPLATE_LOCK.open('a') as face:
        acquire(lock);acquire(face);current=epoch()
        state=json.loads(path.read_text()) if path.exists() else {}
        observed=hardware(port,0)
        if action!='status':
            if payload['epoch']!=current:raise ValueError('BCM lifetime changed')
            groups=json.loads(AGGREGATES.read_text()).get('groups',{}) if AGGREGATES.exists() else {}
            if any(g.get('phase')!='stopped' and port in g.get('ports',[]) for g in groups.values()):
                raise ValueError('Physical port belongs to an aggregate owner')
            owned=state.get('epoch')==current and state.get('enabled') and not state.get('pending')
            if observed['enabled'] and not owned:raise ValueError('Unowned or uncertain physical redirect; explicit recovery required')
            if action=='start':
                before=None
                if not observed['enabled']:
                    before={p['port']:p for p in call({'op':'port.list'})['ports']}[MAPPING[port]]['enabled']
                    if type(before) is not bool:raise ValueError('Physical administrative state unavailable')
                    if before:call({'op':'port.set','port':MAPPING[port],'enable':False})
                    rows={p['port']:p for p in call({'op':'port.list'})['ports']}
                    if rows[MAPPING[port]]['enabled'] is not False:raise RuntimeError('Physical withdrawal unverified')
                    # An ambiguous allocation stays down, with the fabric's
                    # durable pending journal. Never retry an uncertain write.
                    ensure([port],current)
                    if epoch()!=current:raise RuntimeError('BCM lifetime changed during queue preparation')
                state=dict(port=port,epoch=current,dp_boot_id=payload['dp_boot_id'],enabled=False,pending='start')
                wan.atomic(path,state);observed=hardware(port,1)
                if before:
                    call({'op':'port.set','port':MAPPING[port],'enable':True})
                    rows={p['port']:p for p in call({'op':'port.list'})['ports']}
                    if rows[MAPPING[port]]['enabled'] is not True:raise RuntimeError('Physical administrative restoration unverified')
            else:
                if observed['enabled'] and state.get('dp_boot_id')!=payload['dp_boot_id']:
                    raise ValueError('Refusing stale DP owner cleanup')
                state=dict(port=port,epoch=current,dp_boot_id=payload['dp_boot_id'],enabled=bool(observed['enabled']),pending='stop')
                wan.atomic(path,state)
                call({'op':'port.set','port':MAPPING[port],'enable':False})
                rows={p['port']:p for p in call({'op':'port.list'})['ports']}
                if rows[MAPPING[port]]['enabled'] is not False:raise RuntimeError('Physical withdrawal unverified')
                observed=hardware(port,2)
            if epoch()!=current:raise RuntimeError('BCM lifetime changed during physical attachment')
            state.update(enabled=action=='start',pending=None);wan.atomic(path,state)
        ready=bool(state.get('epoch')==current and state.get('enabled') and not state.get('pending') and
                   observed==dict(header=11,wan_queues=8,trunk_queues=8,destination=24,enabled=1))
        return dict(port=port,epoch=current,ready=ready,state=state,hardware=observed)


if __name__=='__main__':print(json.dumps(execute(sys.argv[1],json.load(sys.stdin))))
