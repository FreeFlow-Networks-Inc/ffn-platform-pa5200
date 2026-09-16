#!/usr/bin/env python3
"""Single CP owner for WAN1/BCM28 -> BCM24. Never allocates SDK resources."""
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import subprocess
import time
import uuid
from ffn_copper_forwarding import epoch
from ffn_faceplate import call

STATE=Path('/etc/ffn/wan-forwarding.json')
PROOF=Path('/etc/ffn/wan-qualified.json')
LOCK=Path('/run/ffn-wan-forwarding.lock')
SCRIPT=Path('/opt/ffn-compat/tmp/bcmcfg/ffn_bcm_forward_test.c')
RECIPE=r'''
int ffn_wan_queues(int unit,int port,int numq,uint32 flags,int gport,void *data) {
 int rv; int *queues=data;
 if(BCM_GPORT_IS_UCAST_QUEUE_GROUP(gport)) {
  rv=bcm_cosq_gport_get(unit,gport,&port,&numq,&flags); if(rv)return rv;
  port=port & 0x7ff; if(port<37)queues[port]=queues[port]+numq;
 }
 return 0;
}
{
 int q[37]={0}; int rv; int header; int dst; int enabled; int mode=MODE;
 rv=bcm_cosq_gport_traverse(0,ffn_wan_queues,q);
 if(rv==0)rv=bcm_switch_control_port_get(0,24,bcmSwitchPortHeaderType,&header);
 if(mode==1 && rv==0 && (header!=11 || q[28]!=8 || q[24]!=8))rv=-1;
 if(rv==0)rv=bcm_port_force_forward_get(0,28,&dst,&enabled);
 if(mode && rv==0 && enabled && dst!=24)rv=-8;
 if(mode && rv==0)rv=bcm_port_force_forward_set(0,28,24,mode==1);
 if(rv==0)rv=bcm_port_force_forward_get(0,28,&dst,&enabled);
 if(mode && rv==0 && (enabled!=(mode==1) || (enabled && dst!=24)))rv=-1;
 printf("FFN_WAN_STATE header=%d wanq=%d trunkq=%d dst=%d enabled=%d rv=%d\n",header,q[28],q[24],dst,enabled,rv);
 if(rv==0)printf("FFN_WAN_DONE\n");
}
'''


def atomic(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    with temp.open('w') as out:json.dump(value,out);out.flush();os.fsync(out.fileno())
    temp.replace(path)
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def parse(value):
    if not value.get('ok') or not value.get('completed') or value.get('truncated'):
        raise RuntimeError('WAN SDK operation incomplete')
    lines=value.get('markers',[])
    matches=[re.fullmatch(r'FFN_WAN_STATE header=(\d+) wanq=(\d+) trunkq=(\d+) dst=(\d+) enabled=([01]) rv=0',line) for line in lines]
    matches=[m for m in matches if m]
    if len(matches)!=1 or not lines or lines[-1]!='FFN_WAN_DONE':
        raise RuntimeError('WAN hardware readback failed')
    return dict(zip(('header','wan_queues','trunk_queues','destination','enabled'),map(int,matches[0].groups())))


def hardware(mode):
    with open('/run/ffn-forward-test.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        before=SCRIPT.read_bytes() if SCRIPT.exists() else None
        try:
            SCRIPT.write_text(RECIPE.replace('MODE',str(mode)))
            return parse(call({'op':'cint.run','script':SCRIPT.name,'timeout':30}))
        finally:
            if before is None:SCRIPT.unlink(missing_ok=True)
            else:SCRIPT.write_bytes(before)


def canonical_uuid(value):
    if not isinstance(value,str) or str(uuid.UUID(value))!=value:
        raise ValueError('canonical UUID required')
    return value


def watchdog(token):
    # CP-owned cleanup survives loss of the MP or its SSH session. Failed SDK
    # cleanup retries, retaining pending state and withholding readiness.
    subprocess.run(['systemd-run','--quiet','--unit=ffn-wan-probe-'+token,
        '--on-active=60s','--timer-property=AccuracySec=1s',
        '--property=Restart=on-failure','--property=RestartSec=5s',
        '/usr/bin/python3','/usr/local/sbin/ffn_wan_forwarding.py','expire',token],
        check=True,capture_output=True,text=True,timeout=8)


def verified_report(report,state):
    if not isinstance(report,dict):raise ValueError('probe report required')
    if (report.get('port')!=1 or report.get('bcm_port')!=28
            or report.get('boot_id')!=state['dp_boot_id']
            or report.get('lease_acquired') is not False
            or type(report.get('dhcp_offer_verified')) is not bool
            or type(report.get('discover_sent')) is not int
            or not 1<=report['discover_sent']<=3):
        raise ValueError('invalid WAN probe identity or result')
    return report['dhcp_offer_verified']


def wire_qualified(proof, current, boot_id):
    report=proof.get('report',{})
    return (proof.get('schema')==1 and proof.get('epoch')==current and proof.get('port')==1
            and report.get('boot_id')==boot_id and report.get('port')==1 and report.get('bcm_port')==28
            and type(report.get('discover_sent')) is int and 1<=report['discover_sent']<=3
            and report.get('counters',{}).get('wan_rx',0)>0
            and report.get('return_sources',{}).get('28',0)>0)


def execute(operation,payload=None):
    payload={} if payload is None else payload
    fields={'status':set(),'prepare':{'revision','token','dp_boot_id'},
            'finish':{'token','report'},'expire':{'token'},'abort':{'token'},
            'recover':{'revision'},'start':{'revision','dp_boot_id'},'stop':{'revision'}}
    if operation not in fields or not isinstance(payload,dict) or set(payload)!=fields[operation]:
        raise ValueError('invalid WAN operation or fields')
    for key in ('token','dp_boot_id'):
        if key in payload:canonical_uuid(payload[key])
    with LOCK.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        current=epoch()
        state=json.loads(STATE.read_text()) if STATE.exists() else {'revision':0,'enabled':False,'pending':None}
        proof=json.loads(PROOF.read_text()) if PROOF.exists() else {}
        if 'revision' in payload and (type(payload['revision']) is not int or payload['revision']!=state['revision']):
            raise ValueError('WAN revision conflict')
        if operation in ('expire','abort') and (state.get('token')!=payload['token'] or not state.get('pending')
                                                or state.get('epoch')!=current):
            return {'cleanup_required':False}
        observed=hardware(0)
        if operation=='start':
            if not wire_qualified(proof,current,payload['dp_boot_id']):
                raise RuntimeError('WAN wire mapping must be qualified in this CP/DP lifetime')
            if state.get('pending'):
                raise RuntimeError('Recover the interrupted WAN operation first')
            if observed['enabled'] and not (state.get('enabled') and state.get('epoch')==current
                                            and state.get('dp_boot_id')==payload['dp_boot_id']):
                raise RuntimeError('Refusing to adopt an unowned WAN redirect')
            wanted={'revision':state['revision']+1,'epoch':current,'dp_boot_id':payload['dp_boot_id'],
                    'enabled':False,'pending':'start'}
            atomic(STATE,wanted)
            observed=hardware(1)
            if epoch()!=current:raise RuntimeError('BCM owner changed during WAN attachment')
            state=wanted|{'enabled':True,'pending':None};atomic(STATE,state)
        elif operation=='prepare':
            if state.get('pending') or state.get('enabled') or observed['enabled']:
                raise RuntimeError('WAN path must be stopped and recovered before probing')
            wanted={'revision':state['revision']+1,'epoch':current,'enabled':False,'pending':'prepare',
                    'token':payload['token'],'dp_boot_id':payload['dp_boot_id'],
                    'deadline':time.monotonic()+50}
            watchdog(payload['token'])
            atomic(STATE,wanted)
            observed=hardware(1)
            if epoch()!=current:raise RuntimeError('BCM owner changed during WAN preparation')
            state=wanted|{'enabled':True,'pending':'probe'}
            atomic(STATE,state)
        elif operation!='status':
            if state.get('epoch')!=current:raise RuntimeError('BCM owner changed; stale WAN cleanup refused')
            success=False
            if operation=='finish':
                if state.get('token')!=payload['token'] or state.get('pending')!='probe':
                    raise ValueError('probe token is no longer pending')
                success=verified_report(payload['report'],state)
                if time.monotonic()>state['deadline']:raise RuntimeError('WAN probe deadline expired')
            wanted=state|{'revision':state['revision']+1,'pending':operation}
            atomic(STATE,wanted)
            observed=hardware(2)
            if epoch()!=current:raise RuntimeError('BCM owner changed during WAN cleanup')
            state=wanted|{'enabled':False,'pending':None}
            atomic(STATE,state)
            if operation=='finish':
                proof={'schema':1,'epoch':current,'port':1,'dhcp_offer_verified':success,
                       'probe_token':payload['token'],'report':payload['report']}
                atomic(PROOF,proof)
        if epoch()!=current:raise RuntimeError('BCM owner changed during WAN observation')
        return {'revision':state['revision'],'config':{'revision':state['revision']},'epoch':current,'state':state,'hardware':observed,
                'scope':[1],'qualified':proof.get('epoch')==current and proof.get('dhcp_offer_verified') is True,
                'ready':{'1':bool(state.get('epoch')==current and state.get('enabled') and not state.get('pending')
                     and wire_qualified(proof,current,state.get('dp_boot_id')) and observed=={
                     'header':11,'wan_queues':8,'trunk_queues':8,'destination':24,'enabled':1})},
                'qualification':proof}


if __name__=='__main__':
    operation=sys.argv[1] if len(sys.argv)>1 else 'status'
    if operation=='expire' and len(sys.argv)==3:payload={'token':sys.argv[2]}
    else:
        raw=sys.stdin.buffer.read(65537)
        if len(raw)>65536:raise ValueError('WAN request too large')
        payload=json.loads(raw) if raw.strip() else {}
    print(json.dumps(execute(operation,payload)))
