#!/usr/bin/env python3
"""MP control worker for committed physical-interface packet attachments."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from xml.etree import ElementTree as ET
from aggregate_activation import CP,DP,atomic

RUNNING=Path('/var/lib/ffn-ngfw/config/running-config.xml')
INTENT=Path('/var/lib/ffn-ngfw/physical-ports-intent.json')


def revision(cp,dp):
    observed=dict(cp=cp,dp=dict(running=dp['running'],boot_id=dp['boot_id']))
    return int(hashlib.sha256(json.dumps(observed,sort_keys=True).encode()).hexdigest()[:13],16)


def remote(role,action,port,payload):
    command=('python3 /usr/local/sbin/ffn_physical_forwarding.py '+action if role=='cp' else
             'python3 /usr/local/sbin/ffn_physical_runtime.py '+action+' '+str(port))
    result=subprocess.run((CP if role=='cp' else DP)+[command],input=json.dumps(payload),text=True,capture_output=True,timeout=45)
    if result.returncode:raise RuntimeError(role+' physical attachment failed: '+result.stderr[-1200:])
    return json.loads(result.stdout)


def execute(action,payload,call=remote):
    if action=='lookup':action='status'
    if action not in ('status','validate','apply') or not isinstance(payload,dict):raise ValueError('Invalid physical request')
    if action=='status' and not payload:
        if not INTENT.exists():return dict(config={'revision':0},ports=[])
        payload={'port':json.loads(INTENT.read_text())['port']}
    required={'port'} if action=='status' else {'port','running_revision','operation','revision'}
    if set(payload)!=required or type(payload.get('port')) is not int or not 2<=payload['port']<=24:
        raise ValueError('Valid independent physical port required')
    port=payload['port'];cp=call('cp','status',port,{'port':port});dp=call('dp','status',port,{})
    if action=='status':
        return dict(cp=cp,dp=dp,config={'revision':revision(cp,dp)})
    if type(payload['revision']) is not int or payload['revision']!=revision(cp,dp):raise ValueError('Physical owner revision changed')
    source=RUNNING.read_bytes()
    if payload['running_revision']!=hashlib.sha256(source).hexdigest():raise ValueError('Committed configuration changed')
    entries=[e for e in ET.fromstring(source).findall('./devices/entry/network/interface/ethernet/entry') if e.get('name')=='ethernet1/'+str(port)]
    active=len(entries)==1 and entries[0].find('layer3') is not None and entries[0].find('aggregate-group') is None
    if payload['operation']!=('attach' if active else 'detach'):raise ValueError('Attachment must match committed physical interface settings')
    if action=='validate':return dict(validated=True)
    atomic(INTENT,payload)
    identity=dict(port=port,epoch=cp['epoch'],dp_boot_id=dp['boot_id'])
    if active:
        if not (cp['ready'] and cp['state'].get('dp_boot_id')==dp['boot_id'] and dp['running']):
            if RUNNING.read_bytes()!=source:raise ValueError('Committed configuration changed')
            cp=call('cp','start',port,identity)
            try:dp=call('dp','start',port,{'boot_id':dp['boot_id']})
            except BaseException:
                call('cp','stop',port,identity);raise
        if not cp['ready'] or not dp['running']:raise RuntimeError('Physical attachment readback incomplete')
    else:
        dp=call('dp','stop',port,{'boot_id':dp['boot_id']})
        cp=call('cp','stop',port,identity)
    if RUNNING.read_bytes()!=source:raise ValueError('Committed configuration changed during physical apply')
    return dict(applied=True,cp=cp,dp=dp,hardware_offload=False)


if __name__=='__main__':print(json.dumps(execute(sys.argv[1],json.load(sys.stdin))))
