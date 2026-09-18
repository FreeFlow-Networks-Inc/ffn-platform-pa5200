#!/usr/bin/env python3
"""Fixed MP daemon adapters. Only the daemon executes these hardware helpers."""
import asyncio
import json
import re
import sys
from pathlib import Path
from hardware_backend import Controller, COMMANDS

FIELDS={'phy':{'revision','phy','speed'},'bcm':{'revision','operation','acknowledge_link_outage'},'network':{'revision','ports','routes','vrfs','rules'},
        'fe100-policy':{'revision','digest'},
        'overlay':{'revision','links'},'inspection':{'revision','mode','ports','literal','detectors'},
        'faceplate':{'revision','port','enabled','speed','restart_autoneg','restore_pair_map'},'thermal':{'revision','operation'}}


def require_front_mode(port,config=Path('/var/lib/ffn-ngfw/config/running-config.xml')):
    """A hardware enable cannot bypass the committed interface mode."""
    from aggregate_config import parse
    root=parse(config.read_bytes())
    entry=root.find("./devices/entry[@name='localhost.localdomain']/network/interface/ethernet/entry[@name='ethernet1/%d']"%port)
    if (entry is None or entry.findtext('link-state','auto')=='down' or
        not (any(entry.find(mode) is not None for mode in ('layer3','layer2','virtual-wire','tap','ha','decrypt-mirror')) or entry.findtext('aggregate-group','').strip())):
        raise ValueError('Front interface is None/disabled; select and commit an interface mode before enabling its link')


async def execute(resource, action, payload, backend=None):
    backend=backend or Controller()
    if resource == 'lacp':
        if action == 'status':
            if payload: raise ValueError('status takes no payload')
            return await backend.run('lacp','status')
        if action not in ('apply','validate'): raise ValueError('unsupported LACP action')
        data=dict(payload)
        operation=data.pop('operation', None)
        fields={'revision','groups'} if operation=='set' else {'revision','group'}
        if operation not in ('set','activate','deactivate') or set(data)!=fields:
            raise ValueError('invalid LACP operation')
        observed=await backend.run('lacp','status')
        if type(data['revision']) is not int or data['revision']!=observed['config']['revision']:
            raise ValueError('revision conflict')
        if operation=='activate' and not observed['capabilities']['activation_supported']:
            raise ValueError('LACP backend is not qualified')
        if action=='validate': return {'validated':True}
        return await backend.run('lacp',operation,data)
    if action in ('apply','validate'):
        if resource not in FIELDS or set(payload)-FIELDS[resource]: raise ValueError('unsupported fields')
        if type(payload.get('revision')) is not int or payload['revision']<0: raise ValueError('revision required')
        if resource=='thermal':
            if set(payload)!=FIELDS[resource] or payload['operation'] not in ('auto','full') or payload['revision']!=0:
                raise ValueError('invalid thermal operation')
        else:
            observed=await backend.run(resource,'status')
            revision=observed.get('config',observed).get('revision')
            if revision!=payload['revision']: raise ValueError('revision conflict; refresh state')
            if resource=='fe100-policy':
                if set(payload)!=FIELDS[resource] or not isinstance(payload['digest'],str) or not re.fullmatch('[0-9a-f]{64}',payload['digest']):
                    raise ValueError('exact candidate digest required')
            if resource=='phy':
                if set(payload)!=FIELDS['phy'] or type(payload['phy']) is not int or payload['phy'] not in range(16,20) or payload['speed'] not in ('auto','100','1000','10000'):
                    raise ValueError('Invalid PHY configuration')
                if observed.get('saved',{}).get('pending') or not observed['phys'][payload['phy']-16].get('ready'):
                    raise ValueError('PHY unavailable or operation pending')
            if resource=='bcm':
                if set(payload)!=FIELDS['bcm'] or payload.get('operation') not in ('start','stop','restart') or payload.get('acknowledge_link_outage') is not True:
                    raise ValueError('Invalid BCM service request')
                if not observed.get('operation_complete'):raise ValueError('BCM operation still pending')
            if resource=='faceplate':
                if (not {'port','revision'}<=set(payload) or not {'enabled','speed','restart_autoneg','restore_pair_map'}&set(payload) or type(payload['port']) is not int or
                        not 1<=payload['port']<=24 or ('enabled' in payload and type(payload['enabled']) is not bool)):
                    raise ValueError('invalid faceplate change')
                if payload.get('enabled') is True:require_front_mode(payload['port'])
                port=next((p for p in observed.get('ports',[]) if p['port']==payload['port']),{})
                if port.get('media')=='copper' and (port.get('admin_configuration') is False or port.get('phy_pending')):
                    raise ValueError('Copper control unavailable or pending')
                if 'restart_autoneg' in payload and (payload['restart_autoneg'] is not True or set(payload)!={'revision','port','restart_autoneg'}
                        or not port.get('renegotiate_configuration') or not port.get('enabled') or observed.get('saved',{}).get('pending')):
                    raise ValueError('Renegotiation requires an enabled, ready copper port and a standalone request')
                if 'restore_pair_map' in payload and (payload['restore_pair_map'] is not True or set(payload)!={'revision','port','restore_pair_map'}
                        or not port.get('pair_map_recovery') or not port.get('enabled') or observed.get('saved',{}).get('pending')):
                    raise ValueError('Pair map recovery requires an enabled, down copper port and a standalone request')
                if 'speed' in payload:
                    if not port.get('speed_configuration') or payload['speed'] not in ['auto']+[str(v) for v in port.get('supported_speeds',[])]:
                        raise ValueError('Unsupported link speed')
            if resource=='inspection':
                from ffn_inspection import validate
                validate(payload)
        if action=='validate':
            if resource=='network':return await backend.run('network','validate',payload)
            return {'validated':True}
        actual='patch' if resource=='network' else payload['operation'] if resource=='thermal' else 'set'
        return await backend.run(resource,actual,None if resource=='thermal' else payload)
    if action not in ('status','lookup') or (resource,action) not in COMMANDS:
        raise ValueError('unsupported operation')
    if action=='status' and payload: raise ValueError('status takes no payload')
    if action=='lookup' and (set(payload)-{'dst','vrf'} or not isinstance(payload.get('dst'),str)):
        raise ValueError('invalid lookup')
    return await backend.run(resource,action,payload or None)


if __name__=='__main__':
    try:
        result=asyncio.run(execute(sys.argv[1],sys.argv[2],json.load(sys.stdin)))
        print(json.dumps(result))
    except Exception:
        # Do not send SSH diagnostics or configuration contents to clients.
        print(json.dumps({'error':'MP hardware adapter rejected or could not complete the operation'}))
        sys.exit(2)
