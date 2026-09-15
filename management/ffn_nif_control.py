#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""CP-owned FE100 link service activation, distinct from forwarding readiness."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

STATE = Path('/etc/ffn/nif-control.json')
UNIT = 'ffn-fe100-links.service'


def service():
    result = subprocess.run(['/usr/bin/systemctl', 'show', UNIT, '--property=LoadState,ActiveState,SubState'],
                            capture_output=True, text=True, check=True, timeout=10)
    return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)


def links():
    try:
        result = subprocess.run(['/usr/bin/python3','/usr/local/sbin/ffn_fe100_links_status.py'],
                                capture_output=True,text=True,check=True,timeout=12)
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {'physical_links_up':None,'error':'Physical link status unavailable'}


def validate(cfg, request, observed):
    if (not isinstance(request, dict) or set(request) != {'revision','enabled'} or
            type(request['revision']) is not int or request['revision'] != cfg['revision']):
        raise ValueError('revision conflict or invalid request')
    if request['enabled'] is not True:
        raise ValueError('NIF shutdown is not supported by the commissioned link adapter')
    if observed.get('LoadState') != 'loaded':
        raise ValueError('commissioned FE100 link service is not installed')
    return dict(request, revision=cfg['revision']+1)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['status','validate','apply'])
    args=p.parse_args()
    with open('/run/ffn-nif-control.lock','w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cfg=json.loads(STATE.read_text()) if STATE.exists() else {'revision':0,'enabled':False}
        observed=service()
        if args.action=='status':
            physical=links() if observed.get('ActiveState')=='active' else {'physical_links_up':None}
            result={'config':cfg,'service':observed,'physical':physical,
                    'capabilities':{'enable':observed.get('LoadState')=='loaded','disable':False,'hardware_forwarding_verified':False}}
        else:
            proposed=validate(cfg,json.load(sys.stdin),observed)
            if args.action=='validate': result={'validated':True,'config':proposed}
            else:
                # The commissioned service owns register writes, ABI pinning and
                # dependencies. Never execute a supplied command or restart it.
                subprocess.run(['/usr/bin/systemctl','start','--no-block',UNIT],check=True,
                               capture_output=True,timeout=10)
                STATE.parent.mkdir(parents=True,exist_ok=True)
                temp=STATE.with_suffix('.tmp')
                with temp.open('w') as stream:
                    json.dump(proposed,stream);stream.flush();os.fsync(stream.fileno())
                temp.replace(STATE)
                directory=os.open(STATE.parent,os.O_RDONLY|os.O_DIRECTORY)
                try: os.fsync(directory)
                finally: os.close(directory)
                result={'config':proposed,'activation':'pending','link_readiness':'not verified'}
        print(json.dumps(result))


if __name__=='__main__':
    try: main()
    except ValueError as error:
        print(json.dumps({'error':str(error)}))
        raise SystemExit(2)
