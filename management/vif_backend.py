#!/usr/bin/env python3
"""Selected MP daemon resource: VIF status, validation and assignment control."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys


def remote(op,payload):
    if op not in ('status','check','set','start','stop','recover','links'):raise ValueError('invalid DP operation')
    argv=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',
          '-o','UserKnownHostsFile=/etc/ffn-ngfw/plane_boot_known_hosts',
          '-o','ProxyCommand=ssh -F /etc/ffn-ngfw/ssh-cp.conf -W %h:%p ffn-cp',
          'root@127.1.2.2','python3 /usr/local/sbin/ffn_vif_runtime.py '+op]
    result=subprocess.run(argv,input=json.dumps(payload),text=True,capture_output=True,timeout=65)
    if result.returncode:raise RuntimeError('DP VIF operation failed; refresh status: '+result.stderr[-1200:])
    return json.loads(result.stdout)


def barrier(data):
    path=Path(__file__).with_name('policy_guard.py')
    spec=importlib.util.spec_from_file_location('ffn_vif_policy_guard',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module.before_commit(data)


def faceplate():
    result=subprocess.run(['/usr/local/sbin/ffn-faceplate','status'],text=True,capture_output=True,timeout=8)
    if result.returncode:raise RuntimeError('faceplate observation failed')
    return json.loads(result.stdout)['ports']


def observe(call,read_links):
    result=call('status',{})
    if result.get('running') and result.get('link_token'):
        try:
            return call('links',{'token':result['link_token'],'ports':read_links()})
        except (OSError,ValueError,RuntimeError,KeyError,subprocess.TimeoutExpired) as e:
            # The DP lease expires against its original challenge time even
            # when SSH or the CP hangs; an old response cannot revive carrier.
            result=call('status',{})
            result['link_observation_error']=str(e)[:512]
    return result


def execute(action,payload,call=remote,drain=barrier,read_links=faceplate):
    if action=='status':
        if payload:raise ValueError('status takes no payload')
        return observe(call,read_links)
    if action not in ('validate','apply') or not isinstance(payload,dict):raise ValueError('invalid VIF action')
    operation=payload.get('operation')
    if operation not in ('set','start','stop','recover'):raise ValueError('invalid VIF operation')
    expected={'operation','revision','vifs'} if operation=='set' else {'operation','revision'}
    if set(payload)!=expected or type(payload['revision']) is not int:raise ValueError('invalid VIF fields')
    observed=call('status',{})
    if payload['revision']!=observed['config']['revision']:raise ValueError('VIF revision conflict')
    request={k:v for k,v in payload.items() if k!='operation'}
    if operation=='set':call('check',request)
    if action=='validate':return {'validated':True}
    invalidation=drain(json.dumps(payload,sort_keys=True,separators=(',',':')).encode())
    result=call(operation,request if operation=='set' else {})
    return result|{'policy_barrier':invalidation}


if __name__=='__main__':
    try:
        raw=sys.stdin.buffer.read(65537)
        if len(raw)>65536:raise ValueError('VIF request too large')
        print(json.dumps(execute(sys.argv[1],json.loads(raw) if raw.strip() else {})))
    except ValueError as e:
        print(json.dumps({'error':str(e)[:512]}));sys.exit(2)
