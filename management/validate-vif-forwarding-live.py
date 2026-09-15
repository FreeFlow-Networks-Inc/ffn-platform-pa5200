#!/usr/bin/env python3
"""MP-controlled L2/IPv4/IPv6 VIF wire test; only the 5--13 DAC, with cleanup."""
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

neighbors='--neighbors' in sys.argv
output=Path('/tmp/VIF-'+('NEIGHBOR' if neighbors else 'L2-L3')+'-FORWARDING-20260915.json')
report={'started':time.time(),'steps':[]}

def record(label,result):
    report['steps'].append({'label':label,'result':result})
    output.write_text(json.dumps(report,indent=2)+'\n')
    print(label+': '+json.dumps(result),flush=True)

def call(resource,action,payload=None):
    request={'v':1,'id':str(uuid.uuid4()),'resource':resource,'action':action,'payload':payload or {}}
    reply=asyncio.run(rpc('/run/ffn-plane-mp/control.sock',request))
    if not reply.get('ok'):raise RuntimeError(json.dumps(reply))
    return reply['result']

def vif(op,bindings=None):
    state=call('vifs','status')
    payload={'revision':state['config']['revision'],'operation':op}
    if op=='set':payload['vifs']=bindings
    result=call('vifs','apply',payload);record('vif_'+op,result);return result

def network(**fields):
    state=call('network','status')
    result=call('network','apply',{'revision':state['config']['revision'],**fields})
    record('network_apply',result);return result

def dp(*args):
    return subprocess.run(['python3','/tmp/ffn-dp-ssh.py',*args],capture_output=True,text=True,timeout=65)

def checked(*args):
    result=dp(*args)
    if result.returncode:raise RuntimeError(result.stdout+result.stderr)
    return result.stdout

def probe(mode,expected=4):
    result=dp('python3','/usr/local/sbin/validate_vif_forwarding.py',mode,'--expect',str(expected),
              *(['--resolve-neighbors'] if neighbors else []))
    record('wire_'+mode,json.loads(result.stdout))
    if result.returncode:raise RuntimeError('wire test failed: '+result.stderr)

def face(port,enabled,speed):
    state=call('faceplate','status')
    result=call('faceplate','apply',{'revision':state['revision'],'port':port,'enabled':enabled,'speed':speed})
    record('front_'+str(port),{'activation':result.get('activation')})

def bridge_ready():
    deadline=time.monotonic()+45
    while time.monotonic()<deadline:
        links=json.loads(checked('ip','-n','ffn-data','-d','-j','link'))
        states={p['ifname']:p.get('linkinfo',{}).get('info_slave_data',{}).get('state') for p in links}
        if all(states.get(n) in (3,'forwarding') for n in ('fv4001','fv4002')):return
        time.sleep(2)
    raise RuntimeError('VIF bridge ports did not reach forwarding state')

before=call('vifs','status');net_before=call('network','status')['config']
front={p['port']:p for p in call('faceplate','status')['ports'] if p['port'] in (5,13)}
if before['running'] or before['config']['vifs'] or before['recovery_required']:
    raise RuntimeError('lab requires empty stopped VIF runtime')
if set(front)!={5,13} or 'vrf-viflab' in net_before.get('vrfs',{}) or 4090 in net_before.get('vrfs',{}).values():
    raise RuntimeError('test resources unavailable')
if any(set(p.get('vlans',[])) & {3900,3903} for p in net_before['ports'].values()):
    raise RuntimeError('test bridge VLAN already configured')
record('before',{'network':net_before,'vifs':before,'front':front})
bindings={n:{'port':p,'vlan':v,'enabled':True,'network':{'mode':'l2','vlans':[3900],'pvid':3900}}
          for n,p,v in [('fv4001',13,3901),('fv4002',5,3902)]}
changed=[]
try:
    for port in (5,13):changed.append(port);face(port,True,'10000')
    current={p['port']:p for p in call('faceplate','status')['ports'] if p['port'] in (5,13)}
    if not all(p['enabled'] and p['link'] for p in current.values()):raise RuntimeError('DAC link unavailable')
    if not neighbors:
        vif('set',bindings);vif('start');bridge_ready();probe('l2')
        bindings['fv4002']['network']={'mode':'l2','vlans':[3903],'pvid':3903}
        vif('set',bindings);bridge_ready();probe('l2',0)
    network(vrfs={**net_before.get('vrfs',{}),'vrf-viflab':4090})
    for name,subnet in [('fv4001',201),('fv4002',202)]:
        bindings[name]['network']={'mode':'l3','vrf':'vrf-viflab',
            'addresses':['198.18.%d.1/24'%subnet,'2001:db8:%d::1/64'%subnet]}
    vif('set',bindings)
    if neighbors:vif('start')
    for name,subnet,mac in [('fv4001',201,'02:ff:00:00:00:01'),('fv4002',202,'02:ff:00:00:00:02')]:
        for address in ('198.18.%d.2'%subnet,'2001:db8:%d::2'%subnet):
            if not neighbors:
                checked('ip','-n','ffn-data','neigh','replace',address,'lladdr',mac,'dev',name,'nud','permanent')
    routes=[];rules=[]
    for name,subnet in [('fv4001',201),('fv4002',202)]:
        routes += [{'dst':'198.19.%d.0/24'%subnet,'via':'198.18.%d.2'%subnet,'dev':name,'table':4090},
                   {'dst':'2001:db8:%d::/64'%(subnet+100),'via':'2001:db8:%d::2'%subnet,'dev':name,'table':4090}]
        rules += [{'from':'198.18.%d.0/24'%subnet,'iif':name,'table':4090,'priority':subnet+400},
                  {'from':'2001:db8:%d::/64'%subnet,'iif':name,'table':4090,'priority':subnet+400}]
    network(routes=net_before.get('routes',[])+routes,rules=net_before.get('rules',[])+rules)
    time.sleep(2)
    probe('ipv4');probe('ipv6')
    if neighbors:record('learned_neighbors',json.loads(checked('ip','-n','ffn-data','-j','neigh','show')))
    record('route_lookup',call('network','lookup',{'dst':'198.19.202.2','vrf':'vrf-viflab'}))
    invalid=copy.deepcopy(bindings);invalid['fv4001']['enabled']=False
    current=call('vifs','status')
    try:call('vifs','validate',{'revision':current['config']['revision'],'operation':'set','vifs':invalid})
    except RuntimeError:record('dependent_assignment_rejected',True)
    else:raise AssertionError('VIF changed with dependent routes/policies')
    record('runtime',call('vifs','status'))
    report['passed']=True
finally:
    try:
        network(routes=net_before.get('routes',[]),rules=net_before.get('rules',[]))
        vif('set',before['config']['vifs']);vif('stop')
        network(vrfs=net_before.get('vrfs',{}))
        record('after',{'network':call('network','status')['config'],'vifs':call('vifs','status')})
    finally:
        for port in reversed(changed):face(port,front[port]['enabled'],front[port]['configured_speed'])
        record('front_restored',{p['port']:p for p in call('faceplate','status')['ports'] if p['port'] in (5,13)})
        report['finished']=time.time();output.write_text(json.dumps(report,indent=2)+'\n')
