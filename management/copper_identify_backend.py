#!/usr/bin/env python3
"""MP resource adapter for the fixed CP copper identification controller."""
import json
import subprocess
import sys


def execute(action,payload):
    if action not in ('status','validate','apply'):raise ValueError('invalid action')
    result=subprocess.run(['ssh','-F','/etc/ffn-ngfw/ssh-cp.conf','-o','BatchMode=yes',
        '-o','ConnectTimeout=5','ffn-cp','python3 /usr/local/sbin/ffn_copper_identify.py '+action],
        input=json.dumps(payload),text=True,capture_output=True,timeout=15)
    data=json.loads(result.stdout)
    if result.returncode:raise ValueError(data.get('error','CP identification failed'))
    return data


if __name__=='__main__':
    try:
        raw=sys.stdin.buffer.read(65537)
        if len(raw)>65536:raise ValueError('request too large')
        print(json.dumps(execute(sys.argv[1],json.loads(raw))))
    except Exception as e:print(json.dumps({'error':str(e)[:512]}));sys.exit(2)
