#!/usr/bin/env python3
"""Retired MP resource: the copper VIF driver is gone; copper ports are faceplate ports.

Configure ethernet1/1 through ethernet1/4 like every other front port (Network >
Interfaces); the committed configuration attaches them through `physical-ports`
and `wan-path`, their owner on the DP is ffn-portd, and the CP's PHY/MAC
observation reaches that owner through the ffn-port-links timer. This resource
answers every request with that direction so an old client learns where to go.
"""
import json
import sys

RETIRED=('The VIF resource is retired: copper ports 1-4 are faceplate ports. Configure ethernet1/N '
         'in Network > Interfaces and commit; the DP owner is ffn-portd and carrier follows the '
         'ffn-port-links observation.')


def execute(action,payload):
    if action=='status':
        return {'retired':True,'reason':RETIRED,'config':{'revision':0,'vifs':{}},'running':False,'forwarding':False,'ports':[]}
    raise ValueError(RETIRED)


if __name__=='__main__':
    try:
        raw=sys.stdin.buffer.read(65537)
        print(json.dumps(execute(sys.argv[1],json.loads(raw) if raw.strip() else {})))
    except ValueError as e:
        print(json.dumps({'error':str(e)[:512]}));sys.exit(2)
