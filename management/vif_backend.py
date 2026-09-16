#!/usr/bin/env python3
"""Selected MP daemon resource: VIF status, validation and assignment control."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor


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
    with ThreadPoolExecutor(max_workers=1) as worker:
        pending=worker.submit(copper_forwarding,'status')
        result=subprocess.run(['/usr/local/sbin/ffn-faceplate','status'],text=True,capture_output=True,timeout=8)
        ready=pending.result()['ready']
    if result.returncode:raise RuntimeError('faceplate observation failed')
    rows=json.loads(result.stdout)['ports']
    for row in rows:
        if row['port']<=4:row['packet_path_ready']=ready.get(str(row['port']),False)
    return rows


def copper_forwarding(operation):
    if operation not in ('status','start','stop','recover'):raise ValueError('invalid copper operation')
    result=subprocess.run(['ssh','-F','/etc/ffn-ngfw/ssh-cp.conf','-o','BatchMode=yes','ffn-cp',
        'python3 /usr/local/sbin/ffn_copper_forwarding.py '+operation],capture_output=True,text=True,timeout=40)
    if result.returncode:raise RuntimeError('CP copper forwarding failed: '+result.stderr[-1000:])
    return json.loads(result.stdout)


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


def execute(action,payload,call=remote,drain=barrier,read_links=faceplate,forward=copper_forwarding):
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
    bindings=payload['vifs'] if operation=='set' else observed['config'].get('vifs',{})
    copper=any(b.get('enabled') and b.get('port') in (3,4) for b in bindings.values())
    if copper and (operation=='start' or operation=='set' and observed.get('running')):forward('start')
    result=call(operation,request if operation=='set' else {})
    if operation in ('stop','recover'):
        state=forward('status')
        owned=state['state']
        if (owned.get('enabled') or owned.get('pending')) and owned.get('epoch')==state['epoch']:
            forward('recover' if owned.get('pending') else 'stop')
    return result|{'policy_barrier':invalidation}


if __name__=='__main__':
    try:
        raw=sys.stdin.buffer.read(65537)
        if len(raw)>65536:raise ValueError('VIF request too large')
        print(json.dumps(execute(sys.argv[1],json.loads(raw) if raw.strip() else {})))
    except ValueError as e:
        print(json.dumps({'error':str(e)[:512]}));sys.exit(2)
