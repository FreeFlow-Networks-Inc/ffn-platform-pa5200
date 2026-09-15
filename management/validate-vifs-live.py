#!/usr/bin/env python3
"""MP lab qualification; use only commissioned 5--13 DAC, restore assignments."""
import asyncio
import copy
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid
sys.path.insert(0,'/opt/ffn-ngfw-v2')
from ffn_plane_api import rpc

output=Path('/tmp/VIF-PHYSICAL-VALIDATION-20260915.json')
report={'started':time.time(),'steps':[]}
def record(label,result):
    report['steps'].append({'label':label,'result':result})
    output.write_text(json.dumps(report,indent=2)+'\n');print(label+': '+json.dumps(result),flush=True)
def call(action,payload):
    req={'v':1,'id':str(uuid.uuid4()),'resource':'vifs','action':action,'payload':payload}
    result=asyncio.run(rpc('/run/ffn-plane-mp/control.sock',req))
    if not result.get('ok'):raise RuntimeError(json.dumps(result))
    return result['result']
def change(op,vifs=None):
    current=call('status',{})
    payload={'operation':op,'revision':current['config']['revision']}
    if op=='set':payload['vifs']=vifs
    result=call('apply',payload);record(op,result);return result
def burst(sender,receiver,expected):
    argv=['python3','/tmp/ffn-dp-ssh.py','python3','/usr/local/sbin/validate_vif_packets.py',sender,receiver]
    result=subprocess.run(argv,capture_output=True,text=True,timeout=35)
    if result.returncode:raise RuntimeError(result.stderr)
    data=json.loads(result.stdout);record('physical_packet_test',data)
    assert data['received']==expected,data
    assert data['wire_received']==4,data

def face(action='status',payload=None):
    r=subprocess.run(['/usr/local/sbin/ffn-faceplate',action],input=json.dumps(payload) if payload else '',text=True,capture_output=True,timeout=40)
    if r.returncode:raise RuntimeError(r.stderr)
    return json.loads(r.stdout)
def face_set(port,enabled,speed):
    current=face();result=face('set',{'revision':current['revision'],'port':port,'enabled':enabled,'speed':speed})
    record('faceplate_'+str(port),{'activation':result.get('activation'),
           'ports':[p for p in result.get('data',{}).get('ports',[]) if p['port'] in (5,13)]})

before=call('status',{});record('before',before)
if before['running'] or before['config']['vifs'] or before['recovery_required']:raise RuntimeError('lab requires empty stopped VIF runtime')
vifs={name:{'port':port,'vlan':3901,'enabled':True,'network':{'mode':'l3','addresses':[]}}
      for name,port in [('fv4001',5),('fv4002',13)]}
front_before={p['port']:p for p in face()['ports'] if p['port'] in (5,13)}
record('front_before',front_before)
changed_front=[]
try:
    for p in (5,13):
        changed_front.append(p);face_set(p,True,'10000')
    observed=face();record('front_test',{'ports':[p for p in observed['ports'] if p['port'] in (5,13)]})
    if not all(p['enabled'] and p['link'] for p in observed['ports'] if p['port'] in (5,13)):
        raise RuntimeError('DAC pair has no link')
    change('set',vifs);change('start')
    if '--disable-only' not in sys.argv:
        burst('fv4001','fv4002',4);burst('fv4002','fv4001',4)
        vifs['fv4002']['vlan']=3902;change('set',vifs)
        burst('fv4001','fv4002',0)
        vifs['fv4001']['vlan']=3902;change('set',vifs)
        burst('fv4001','fv4002',4)
    vifs['fv4002']['enabled']=False;change('set',vifs)
    burst('fv4001','fv4002',0)
    vifs['fv4002']['enabled']=True;change('set',vifs)
    burst('fv4002','fv4001',4)
    report['passed']=True
finally:
    # Persist original empty assignments and stop; do not leave a loop configured.
    try:
        try:change('set',before['config']['vifs'])
        finally:change('stop')
        record('after',call('status',{}))
    finally:
        for p in reversed(changed_front):face_set(p,front_before[p]['enabled'],front_before[p]['configured_speed'])
        record('front_after',{'ports':[p for p in face()['ports'] if p['port'] in (5,13)]})
        output.write_text(json.dumps(report,indent=2)+'\n')
