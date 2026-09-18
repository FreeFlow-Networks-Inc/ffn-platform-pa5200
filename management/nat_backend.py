#!/usr/bin/env python3
"""PA5200 MP worker: fixed SSH transport to core MIPS64eb NAT/tool controllers."""
import json
import subprocess
import sys


def execute(resource,action,payload):
    if resource=='dataplane-tools' and action=='status' and not payload:
        command='python3 /usr/local/lib/ffn/ffn_dp_tools.py status'
    elif resource=='nat' and action in ('status','validate','apply'):
        if action=='status' and payload:raise ValueError('status takes no payload')
        if action!='status' and (set(payload)!={'revision','plan'} or type(payload['revision']) is not int):raise ValueError('NAT requires revision and plan')
        command='python3 /usr/local/lib/ffn/ffn_nat_runtime.py '+action
    else:raise ValueError('Unsupported dataplane resource')
    if action=='apply':
        # Hardware flows must not bypass a new software NAT decision.
        from policy_guard import before_commit
        before_commit(json.dumps(payload,sort_keys=True).encode())
    argv=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=5',
          '-o','UserKnownHostsFile=/etc/ffn-ngfw/plane_boot_known_hosts',
          '-o','ProxyCommand=ssh -F /etc/ffn-ngfw/ssh-cp.conf -W %h:%p ffn-cp',
          'root@127.1.2.2',command]
    result=subprocess.run(argv,input=json.dumps(payload),text=True,capture_output=True,timeout=35)
    if result.returncode:
        if result.returncode==2:
            try:error=json.loads(result.stdout)['error']
            except (ValueError,KeyError):error='Invalid NAT request'
            raise ValueError(error)
        raise RuntimeError('Dataplane outcome unconfirmed: '+result.stderr[-1000:])
    return json.loads(result.stdout)


if __name__=='__main__':
    try:
        raw=sys.stdin.buffer.read(65537)
        if len(raw)>65536:raise ValueError('Dataplane request exceeds 64 KiB')
        print(json.dumps(execute(sys.argv[1],sys.argv[2],json.loads(raw) if raw.strip() else {})))
    except ValueError as error:print(json.dumps({'error':str(error)[:1024]}));raise SystemExit(2)
