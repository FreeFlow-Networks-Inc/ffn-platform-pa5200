#!/usr/bin/env python3
"""MP controld read-only DP -> CP FE100 session observation bridge."""
import json
import subprocess
import sys
import time
import uuid

DP=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=5',
    '-o','UserKnownHostsFile=/etc/ffn-ngfw/plane_boot_known_hosts',
    '-o','ProxyCommand=ssh -F /etc/ffn-ngfw/ssh-cp.conf -W %h:%p ffn-cp','root@127.1.2.2',
    'ip netns exec ffn-data python3 /usr/local/lib/ffn/ffn_session_feed.py']
CP=['/usr/local/sbin/ffn-cp','python3 /usr/local/sbin/ffn_fe100_session_plan.py']


def call(argv,payload):
    result=subprocess.run(argv,input=json.dumps(payload),text=True,capture_output=True,timeout=12)
    if len(result.stdout)>524288:raise ValueError('Session response exceeds limit')
    if result.returncode:
        try:reason=json.loads(result.stdout)['error']
        except (ValueError,KeyError,TypeError):reason='Session observation transport failed'
        raise ValueError(str(reason)[:512])
    return json.loads(result.stdout)


def execute(action,payload):
    if action!='status' or payload!={}:raise ValueError('Session planning supports only empty status requests')
    nonce=str(uuid.uuid4());start=time.monotonic()
    observation=call(DP,{'nonce':nonce})
    if observation.get('nonce')!=nonce or observation.get('available') is not True:
        raise ValueError('Unacknowledged session observation')
    result=call(CP,dict(nonce=nonce,observation=observation))
    elapsed=time.monotonic()-start
    if (elapsed>15 or result.get('nonce')!=nonce or result.get('hardware_admission') is not False or
        result.get('producer')!=observation.get('producer') or result.get('policy')!=observation.get('policy')):
        raise ValueError('Session plan is stale or has mismatched identities')
    return dict(result,round_trip_seconds=round(elapsed,3))


if __name__=='__main__':
    try:
        raw=sys.stdin.buffer.read(4097)
        if len(raw)>4096 or len(sys.argv)!=2:raise ValueError('Invalid session status request')
        print(json.dumps(execute(sys.argv[1],json.loads(raw) if raw.strip() else {})))
    except (ValueError,KeyError,TypeError,OSError,subprocess.TimeoutExpired) as error:
        print(json.dumps({'error':str(error)[:512]}));raise SystemExit(2)
