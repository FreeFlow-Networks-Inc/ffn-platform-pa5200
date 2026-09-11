#!/usr/bin/env python3
"""Fixed MP daemon adapters. Only the daemon executes these hardware helpers."""
import asyncio
import json
import sys
from hardware_backend import Controller, COMMANDS

FIELDS={'network':{'revision','ports','routes','vrfs','rules'},
        'overlay':{'revision','links'},'inspection':{'revision','mode','ports','literal','detectors'},
        'faceplate':{'revision','port','enabled'},'thermal':{'revision','operation'}}


async def execute(resource, action, payload, backend=None):
    backend=backend or Controller()
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
            if resource=='faceplate':
                if (set(payload)!=FIELDS[resource] or type(payload['port']) is not int or
                        not 1<=payload['port']<=24 or type(payload['enabled']) is not bool):
                    raise ValueError('invalid faceplate change')
            if resource=='inspection':
                from ffn_inspection import validate
                validate(payload)
        if action=='validate': return {'validated':True}
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
