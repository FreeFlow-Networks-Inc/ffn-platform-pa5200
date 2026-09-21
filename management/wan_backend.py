#!/usr/bin/env python3
"""MP journal worker for bounded WAN1 wire qualification, without a lease."""
import json
import subprocess
import sys
import uuid
from policy_guard import before_commit


def remote(role, operation, payload):
    cp=['ssh','-F','/etc/ffn-ngfw/ssh-cp.conf','-o','BatchMode=yes','ffn-cp']
    dp=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=5',
        '-o','UserKnownHostsFile=/etc/ffn-ngfw/plane_boot_known_hosts',
        '-o','ProxyCommand=ssh -F /etc/ffn-ngfw/ssh-cp.conf -W %h:%p ffn-cp','root@127.1.2.2']
    if role=='cp' and operation in ('status','prepare','finish','abort','recover','start','stop'):
        argv=cp+['python3 /usr/local/sbin/ffn_wan_forwarding.py '+operation]
    elif role=='dp' and operation in ('status','probe'):
        argv=dp+['python3 /usr/local/sbin/ffn_wan_probe.py'+(' --status' if operation=='status' else '')]
    elif role=='dp' and operation in ('attachment-status','start','stop'):
        argv=dp+['python3 /usr/local/sbin/ffn_wan_runtime.py '+('status' if operation=='attachment-status' else operation)]
    else:raise ValueError('unsupported WAN remote operation')
    result=subprocess.run(argv,input=json.dumps(payload),text=True,capture_output=True,timeout=40)
    if result.returncode:raise RuntimeError(role+' WAN operation failed: '+result.stderr[-1000:])
    try:value=json.loads(result.stdout)
    except ValueError as error:raise RuntimeError('invalid WAN agent response') from error
    if not isinstance(value,dict):raise RuntimeError('invalid WAN agent response')
    return value


def execute(action,payload,call=remote,drain=before_commit):
    if action=='status':
        if payload:raise ValueError('status takes no payload')
        return call('cp','status',{})|{'dp':call('dp','status',{}),'internet_ready':False}
    if (action not in ('validate','apply') or not isinstance(payload,dict)
            or set(payload)!={'operation','revision','expected_boot_id'}
            or payload['operation'] not in ('probe','recover','attach','detach')
            or type(payload['revision']) is not int or payload['revision']<0
            or not isinstance(payload['expected_boot_id'],str)
            or str(uuid.UUID(payload['expected_boot_id']))!=payload['expected_boot_id']):
        raise ValueError('invalid WAN qualification request')
    current=call('cp','status',{})
    if current['revision']!=payload['revision']:raise ValueError('WAN revision conflict')
    dp=call('dp','status',{})
    if dp['boot_id']!=payload['expected_boot_id']:raise ValueError('DP boot identity changed')
    if payload['operation'] in ('attach','detach'):
        attachment=call('dp','attachment-status',{})
        if payload['operation']=='attach' and not attachment['running'] and not dp['fabric_available']:
            raise ValueError('Another dataplane packet owner is active')
        if action=='validate':return {'validated':True}
        barrier=drain(json.dumps(payload,sort_keys=True).encode())
        if payload['operation']=='detach':
            call('dp','stop',{'boot_id':dp['boot_id']})
            result=call('cp','stop',{'revision':current['revision']})
        else:
            proof=current.get('qualification',{})
            qualified=(current.get('wire_qualified') is True
                       and proof.get('report',{}).get('boot_id')==dp['boot_id'])
            if not qualified:
                if attachment['running']:raise ValueError('Withdraw the existing WAN attachment before requalification')
                if (current['state'].get('pending') or current['state'].get('enabled')
                        or current['state'].get('epoch')!=current.get('epoch')):
                    current=call('cp','recover',{'revision':current['revision']})
                token=str(uuid.uuid4())
                try:
                    call('cp','prepare',{'revision':current['revision'],'token':token,'dp_boot_id':dp['boot_id']})
                    report=call('dp','probe',{})
                    current=call('cp','finish',{'token':token,'report':report})
                finally:
                    call('cp','abort',{'token':token})
                if not current.get('wire_qualified'):
                    raise ValueError('WAN wire qualification received no port-1 return traffic; check the physical link and upstream connection')
            result=call('cp','start',{'revision':current['revision'],'dp_boot_id':dp['boot_id']})
            try:attachment=call('dp','start',{'boot_id':dp['boot_id']})
            except BaseException:
                call('cp','stop',{'revision':result['revision']})
                raise
            if not attachment['running'] or not result['ready']['1']:
                raise RuntimeError('WAN attachment readback incomplete')
        return result|{'attachment':attachment,'policy_barrier':barrier,'internet_ready':False}
    if not dp['fabric_available']:raise ValueError('stop the DP packet owner before WAN qualification')
    if payload['operation']=='probe' and (current['state'].get('pending') or current['state'].get('enabled')):
        raise ValueError('recover pending WAN operation first')
    if action=='validate':return {'validated':True}
    barrier=drain(json.dumps(payload,sort_keys=True).encode())
    if payload['operation']=='recover':
        return call('cp','recover',{'revision':payload['revision']})|{'policy_barrier':barrier}
    token=str(uuid.uuid4())
    try:
        call('cp','prepare',{'revision':payload['revision'],'token':token,'dp_boot_id':dp['boot_id']})
        report=call('dp','probe',{})
        result=call('cp','finish',{'token':token,'report':report})
    finally:
        # Also abort after an ambiguous prepare/finish result. Token and BCM
        # epoch fencing prevent this cleanup from touching a newer operation.
        call('cp','abort',{'token':token})
    return result|{'probe':report,'policy_barrier':barrier,'internet_ready':False}


if __name__=='__main__':
    try:
        raw=sys.stdin.buffer.read(65537)
        if len(raw)>65536:raise ValueError('WAN request too large')
        print(json.dumps(execute(sys.argv[1],json.loads(raw) if raw.strip() else {})))
    except ValueError as error:
        print(json.dumps({'error':str(error)[:1024]}));sys.exit(2)
    except (RuntimeError,KeyError,subprocess.TimeoutExpired) as error:
        # Transport/SDK errors may follow a hardware write. Preserve an unknown
        # journal outcome for explicit status inspection and reconciliation.
        print(json.dumps({'error':str(error)[:1024]}));sys.exit(1)
