#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""CP owner of optical aggregate redirects; no queue allocation or SDK restart.

Ingress is forced to the existing OCTEON trunk before links are enabled. A
missing DP owner therefore drops packets instead of falling back to bridging.
The watchdog closes links before removing redirects. Interrupted cleanup stays
journaled and prevents another owner from claiming those ports.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
from ffn_faceplate import call
from ffn_copper_forwarding import epoch
from ffn_wan_forwarding import atomic

STATE=Path('/etc/ffn/aggregate-hardware.json')
LOCK=Path('/run/ffn-aggregate-hardware.lock')
FACEPLATE_LOCK=Path('/run/ffn-faceplate.lock')
SCRIPT=Path('/opt/ffn-compat/tmp/bcmcfg/ffn_bcm_forward_test.c')
PORTS=dict(enumerate((16,1,18,19,6,21,22,23,7,11,36,27,10,29,30,31,32,33,34,35),5))
LEASE=15
RECIPE=r'''
int ffn_ae_q(int unit,int port,int numq,uint32 flags,int gport,void *data) {
 int rv; int *q=data;
 if(BCM_GPORT_IS_UCAST_QUEUE_GROUP(gport)) {
  rv=bcm_cosq_gport_get(unit,gport,&port,&numq,&flags);if(rv)return rv;
  port=port & 0x7ff;if(port<37)q[port]=q[port]+numq;
 } return 0;
}
{
 int q[37]={0};int rv=0;int h=0;int dst=0;int enabled=0;int p=@PORT@;int mode=@MODE@;
 rv=bcm_cosq_gport_traverse(0,ffn_ae_q,q);
 if(rv==0)rv=bcm_switch_control_port_get(0,24,bcmSwitchPortHeaderType,&h);
 if(rv==0)rv=bcm_port_force_forward_get(0,p,&dst,&enabled);
 if(mode==1 && rv==0 && (q[p]!=8 || q[24]!=8 || h!=11 || (enabled && dst!=24)))rv=-1;
 if(mode==2 && rv==0 && enabled && dst!=24)rv=-1;
 if(mode && rv==0)rv=bcm_port_force_forward_set(0,p,24,mode==1);
 if(rv==0)rv=bcm_port_force_forward_get(0,p,&dst,&enabled);
 printf("FFN_AE port=%d header=%d queues=%d trunkq=%d dst=%d enabled=%d rv=%d\n",p,h,q[p],q[24],dst,enabled,rv);
 if(rv==0)printf("FFN_AE_DONE\n");
}
'''


def load():return json.loads(STATE.read_text()) if STATE.exists() else {'groups':{}}


def acquire(lock,seconds=1):
    deadline=time.monotonic()+seconds
    while True:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);return
        except BlockingIOError:
            if time.monotonic()>=deadline:raise RuntimeError('Aggregate hardware lock busy')
            time.sleep(.02)


def hardware(port,mode=0):
    if port not in PORTS or mode not in (0,1,2):raise ValueError('Invalid optical member')
    with open('/run/ffn-forward-test.lock','a') as lock:
        acquire(lock)
        previous=SCRIPT.read_bytes()
        try:
            SCRIPT.write_text(RECIPE.replace('@PORT@',str(PORTS[port])).replace('@MODE@',str(mode)))
            result=call({'op':'cint.run','script':SCRIPT.name,'timeout':30})
        finally:SCRIPT.write_bytes(previous)
    if not result.get('completed') or result.get('truncated'):raise RuntimeError('Aggregate SDK command incomplete')
    rows=[re.fullmatch(r'FFN_AE port=(\d+) header=(\d+) queues=(\d+) trunkq=(\d+) dst=(\d+) enabled=([01]) rv=0',s) for s in result.get('markers',[])]
    rows=[m for m in rows if m]
    if len(rows)!=1 or result.get('markers',[])[-1:]!=['FFN_AE_DONE']:raise RuntimeError('Aggregate hardware readback failed')
    value=dict(zip(('bcm_port','header','queues','trunk_queues','destination','enabled'),map(int,rows[0].groups())))
    if value['bcm_port']!=PORTS[port] or mode and (value['enabled']!=(mode==1) or value['enabled'] and value['destination']!=24):
        raise RuntimeError('Aggregate redirect did not match requested state')
    return value


def validate_identity(payload):
    if not re.fullmatch(r'ae(?:[1-9]|1[0-2])',str(payload.get('group',''))):raise ValueError('Invalid aggregate')
    if str(uuid.UUID(payload.get('token','')))!=payload['token']:raise ValueError('Invalid owner token')


def close(cfg,name):
    state=cfg['groups'][name]
    if state['epoch']!=epoch():raise RuntimeError('BCM lifetime changed; stale cleanup refused')
    state['phase']='stopping';atomic(STATE,cfg)
    # Disable every member before restoring ordinary switching for any member.
    for port in state['ports']:call({'op':'port.set','port':PORTS[port],'enable':False})
    physical={p['port']:p for p in call({'op':'port.list'})['ports']}
    if any(physical.get(PORTS[p],{}).get('enabled') is not False for p in state['ports']):
        raise RuntimeError('Aggregate administrative withdrawal unverified')
    if state.get('offload'):
        from ffn_aggregate_bcm_lag import trunk
        lag=trunk(int(name[2:]))
        if lag['exists']:
            if not state.get('trunk_created') or not set(lag['members'])<=set(state['ports']):
                raise RuntimeError('Trunk ownership uncertain; members disabled, explicit recovery required')
            trunk(lag['tid'],'destroy',old=lag['members'])
        state['trunk_created']=False;atomic(STATE,cfg)
    for port in state['ports']:
        hardware(port,2)
        call({'op':'port.link.set','port':PORTS[port],'speed':state['previous_speeds'][str(port)]})
    state['phase']='stopped';atomic(STATE,cfg)


def execute(action,payload):
    if action not in ('status','prepare','heartbeat','stop','recover','sweep'):raise ValueError('Unknown aggregate hardware operation')
    if action in ('status','sweep'):
        if payload:raise ValueError('Unexpected fields')
    else:validate_identity(payload)
    with LOCK.open('a') as lock:
        # The periodic watchdog and normal heartbeat share this lock. Routine
        # watchdog contention must not be mistaken for a lost hardware owner.
        acquire(lock)
        cfg=load();current=epoch()
        if action=='sweep':
            errors=[]
            for name,state in cfg['groups'].items():
                if state['phase']!='stopped' and state['epoch']==current and time.monotonic()-state['heartbeat']>LEASE:
                    try:
                        with FACEPLATE_LOCK.open('a') as guard:
                            acquire(guard);close(cfg,name)
                    except Exception as error:errors.append(str(error))
            if errors:raise RuntimeError('; '.join(errors))
            return {'expired':True}
        if action=='status':return {'epoch':current,**cfg}
        name=payload['group'];state=cfg['groups'].get(name)
        if action=='recover':
            if (set(payload)!={'group','token','epoch','previous_epoch'} or payload['epoch']!=current
                or not state or state['token']!=payload['token'] or state['epoch']!=payload['previous_epoch']
                or state['epoch']==current):raise ValueError('Fresh BCM epoch and previous owner identity required')
            # A new BCM lifetime invalidates the journal, not necessarily the
            # hardware state. Retire it only after proving an empty baseline.
            with FACEPLATE_LOCK.open('a') as guard:
                acquire(guard)
                physical={p['port']:p for p in call({'op':'port.list'})['ports']}
                for port in state['ports']:
                    if physical.get(PORTS[port],{}).get('enabled') is not False or hardware(port)['enabled']:
                        raise RuntimeError('Previous BCM ownership has not been withdrawn')
                if state.get('offload'):
                    from ffn_aggregate_bcm_lag import trunk
                    if trunk(int(name[2:]))['exists']:raise RuntimeError('Previous BCM trunk still exists')
                if epoch()!=current:raise RuntimeError('BCM lifetime changed during recovery')
                state.update(phase='stopped',recovered_epoch=current);atomic(STATE,cfg)
            return dict(group=name,token=state['token'],epoch=current,phase='stopped')
        if action=='prepare':
            if set(payload)-{'group','token','epoch','ports','speeds','offload'} or not {'group','token','epoch','ports','speeds'}<=set(payload) or payload['epoch']!=current:raise ValueError('Fresh BCM epoch and exact prepare fields required')
            offload=payload.get('offload',False)
            if type(offload) is not bool:raise ValueError('Invalid offload flag')
            ports=payload['ports'];speeds=payload['speeds']
            if (not isinstance(ports,list) or not 2<=len(ports)<=8 or len(set(ports))!=len(ports)
                or any(type(p) is not int or p not in PORTS for p in ports)
                or not isinstance(speeds,dict) or set(speeds)!={str(p) for p in ports}):raise ValueError('Invalid aggregate members')
            if any(g['phase']!='stopped' and (n==name or set(g['ports'])&set(ports)) for n,g in cfg['groups'].items()):
                raise ValueError('Aggregate or member already owned; stop/recover first')
            subprocess.run(['systemctl','is-active','--quiet','ffn-aggregate-watchdog.timer'],check=True,timeout=5)
            with FACEPLATE_LOCK.open('a') as guard:
                acquire(guard)
                physical={p['port']:p for p in call({'op':'port.list'})['ports']};old={}
                for p in ports:
                    live=physical.get(PORTS[p],{});wire=hardware(p);link=call({'op':'port.link.status','port':PORTS[p]})
                    if live.get('enabled') is not False or wire['enabled']:raise ValueError('Members must be administratively down with no existing redirect')
                    if wire['queues']!=8 or wire['trunk_queues']!=8 or wire['header']!=11:raise ValueError('OCTEON trunk/queues not provisioned')
                    if speeds[str(p)] not in ['auto']+[str(v) for v in link['supported_speeds']]:raise ValueError('Unsupported member speed')
                    old[str(p)]=link['configured_speed']
                if offload:
                    from ffn_aggregate_bcm_lag import trunk
                    if trunk(int(name[2:]))['exists']:raise ValueError('BCM trunk already exists; refusing adoption')
                state=dict(token=payload['token'],epoch=current,ports=ports,previous_speeds=old,phase='preparing',heartbeat=time.monotonic(),offload=offload,trunk_created=False)
                cfg['groups'][name]=state;atomic(STATE,cfg)
                try:
                    if offload:
                        trunk(int(name[2:]),'create')
                        state['trunk_created']=True;atomic(STATE,cfg)
                    for p in ports:
                        hardware(p,1)
                        call({'op':'port.link.set','port':PORTS[p],'speed':speeds[str(p)]})
                    for p in ports:call({'op':'port.set','port':PORTS[p],'enable':True})
                    if epoch()!=current:raise RuntimeError('BCM owner changed during activation')
                    state.update(phase='active',heartbeat=time.monotonic());atomic(STATE,cfg)
                except BaseException:
                    close(cfg,name)
                    raise
        else:
            if set(payload)-{'group','token','epoch','egress'} or not {'group','token','epoch'}<=set(payload):raise ValueError('Unexpected fields')
            if not state or state['token']!=payload['token'] or state['epoch']!=current or payload['epoch']!=current:
                raise ValueError('Aggregate ownership changed')
            if action=='stop':
                with FACEPLATE_LOCK.open('a') as guard:
                    acquire(guard);close(cfg,name)
            elif state['phase']!='active' or time.monotonic()-state['heartbeat']>LEASE:
                raise RuntimeError('Aggregate lease expired; explicit recovery required')
        links=[];offload_status=None
        if state['phase']=='active':
            for p in state['ports']:
                wire=hardware(p)
                if wire['enabled']!=1 or wire['destination']!=24:raise RuntimeError('Aggregate ingress ownership lost')
            physical={p['port']:p for p in call({'op':'port.list'})['ports']}
            for p in state['ports']:
                live=physical.get(PORTS[p],{})
                links.append(dict(port=p,up=live.get('enabled') is True and live.get('link') is True,speed_mbps=live.get('speed_mb',0)))
            if state.get('offload'):
                from ffn_aggregate_bcm_lag import trunk
                wanted=payload.get('egress',[])
                if not isinstance(wanted,list) or len(set(wanted))!=len(wanted) or any(type(p) is not int or p not in state['ports'] for p in wanted):raise ValueError('Invalid LACP egress membership')
                # Carrier withdrawal is authoritative even if the last DP
                # report still includes a member. The DP only uses a matching ACK.
                wanted=sorted(set(wanted)&{p['port'] for p in links if p['up']})
                # Keep SPA member indices invariant. This SDK rewrites source
                # metadata even with ingress-disable; a partial trunk would
                # reuse an index for another port while packets are in flight.
                # Degraded groups use the DP software selector instead.
                if wanted!=sorted(state['ports']):wanted=[]
                lag=trunk(int(name[2:]))
                if not lag['exists'] or not state['trunk_created'] or lag['members']!=state.get('egress',[]):raise RuntimeError('BCM trunk ownership changed')
                if wanted!=lag['members']:
                    state['pending_egress']=wanted;atomic(STATE,cfg)
                    lag=trunk(lag['tid'],'set',old=lag['members'],members=wanted)
                    state['egress']=wanted;state.pop('pending_egress',None);atomic(STATE,cfg)
                offload_status=dict(lag,verified=True)
            if epoch()!=current:raise RuntimeError('BCM owner changed during observation')
            state['heartbeat']=time.monotonic();atomic(STATE,cfg)
        return dict(group=name,token=state['token'],epoch=current,phase=state['phase'],links=links,offload=offload_status)


if __name__=='__main__':
    try:
        if sys.argv[1]=='stream':
            for line in sys.stdin:
                request=json.loads(line)
                result=execute('heartbeat',request['payload'])
                print(json.dumps(dict(result,sequence=request['sequence'])),flush=True)
        else:print(json.dumps(execute(sys.argv[1],json.load(sys.stdin))))
    except Exception as error:
        print(json.dumps({'error':str(error)}),flush=True);raise SystemExit(1)
