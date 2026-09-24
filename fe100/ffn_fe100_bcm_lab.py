#!/usr/bin/env python3
"""Serialize isolated FE100 lab recipes with the CP's production BCM owners.

Only the physically isolated front 5/13 lab is supported. Queue preparation is
limited to that loop and its internal FE100/capture destinations. No link
configuration, BCM restart or production member changes are permitted.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import time
from contextlib import contextmanager

ROOT=Path('/var/lib/ffn/fe100')
STATE=ROOT/'bcm-lab-baseline.json'
SCRIPT=Path('/usr/share/broadcom/ffn_bcm_forward_test.c')
TEMPLATE=Path('/usr/local/share/ffn/fe100-lab/ffn_bcm_forward_test.c')
MODES={'offload-rule-delete':30,'offload-rule-counters':31,
       'session-path-inspect':43,'session-path-enable':44,'session-path-restore':45,
       'front5-session-enable':51,'front5-session-restore':52,
       'dsa-front13-create':53,'dsa-front5-create':54,
       'cross13-input-create':55,'cross5-input-create':56,
       'cross13-return-create':57,'cross5-return-create':58,
       'cross13-release':59,'cross5-release':60,'cross13-restore':61,'cross5-restore':62,
       'session-group-absent':63}
IDS=('group','entry','stat','dq1','dq2','presel','trap')
FORWARD_LOCK=Path('/run/ffn-forward-test.lock')


@contextmanager
def locked(path):
    with path.open('a') as lock:
        deadline=time.monotonic()+2
        while True:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise RuntimeError('BCM recipe lock busy')
                time.sleep(.02)
        yield


def render(mode,ids):
    if mode not in MODES or set(ids)-set(IDS):raise ValueError('Unsupported lab recipe')
    if any(type(v)!=int or not -1<=v<2**31 for v in ids.values()):raise ValueError('Invalid hardware ID')
    required={'offload-rule-delete':('group','entry'),
              'offload-rule-counters':('group','entry','stat'),'session-group-absent':('group',)}.get(mode,())
    if any(ids.get(k,-1)<0 for k in required):raise ValueError('Returned hardware IDs required')
    source,n=re.subn(r'int fe100_test = [0-9]+;',f'int fe100_test = {MODES[mode]};',TEMPLATE.read_text())
    if n!=1:raise ValueError('Invalid lab template')
    for k in IDS:source=source.replace(f'int hw_{k} = -1;',f'int hw_{k} = {ids.get(k,-1)};')
    return source


def execute(source,call):
    started=time.monotonic()
    # One SDK operation per shared lock, never a complete queue-preparation
    # sequence. Production owner heartbeats must run between lab operations.
    with locked(FORWARD_LOCK):
        previous=SCRIPT.read_bytes()
        try:
            SCRIPT.write_text(source)
            result=call({'op':'cint.run','script':SCRIPT.name,'timeout':20})
        finally:SCRIPT.write_bytes(previous)
    # flock does not provide FIFO ordering. Immediately reacquiring it for
    # the next lab read can starve production's 20ms retry loop even though
    # each individual SDK call is short. Yield outside the shared lock.
    time.sleep(.1)
    if (not result.get('completed') or result.get('truncated') or
        any('FAIL' in s for s in result.get('markers',[]))):
        raise RuntimeError('BCM lab recipe failed: '+json.dumps(result))
    result['elapsed_seconds']=time.monotonic()-started
    return result


def port_recipe(port,destination=None,enabled=None):
    if port not in (7,16):raise ValueError('Not an isolated lab port')
    change=''
    if destination is not None:
        if type(destination)!=int or not 0<=destination<2**31 or enabled not in (0,1):raise ValueError('Invalid route')
        change=f'if(rv==0)rv=bcm_port_force_forward_set(0,{port},{destination},{enabled});'
    return '''{
 int rv=0;int dst=0;int enabled=0;
 %s
 if(rv==0)rv=bcm_port_force_forward_get(0,%d,&dst,&enabled);
 printf("FFN_LAB_ROUTE port=%%d destination=%%d enabled=%%d rv=%%d\\n",%d,dst,enabled,rv);
 if(rv==0)printf("FFN_DONE\\n");
}
'''%(change,port,port)


def read_port(result,port):
    matches=[re.fullmatch(r'FFN_LAB_ROUTE port=(\d+) destination=(\d+) enabled=([01]) rv=0',s)
             for s in result.get('markers',[])]
    rows=[tuple(map(int,m.groups())) for m in matches if m]
    if len(rows)!=1 or rows[0][0]!=port:raise RuntimeError('Lab route readback failed')
    return dict(port=port,destination=rows[0][1],enabled=rows[0][2])


def save(record,path=None):
    if path is None:path=STATE
    ROOT.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.new')
    with temp.open('w') as f:
        os.chmod(temp,0o600);json.dump(record,f);f.flush();os.fsync(f.fileno())
    os.replace(temp,path)
    fd=os.open(ROOT,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def prepare_queues(call,epoch):
    from ffn_packet_fabric import RECIPE
    path=ROOT/'bcm-lab-queues.json'
    record=json.loads(path.read_text()) if path.exists() else {}
    if record.get('epoch')!=epoch:record=dict(epoch=epoch,ports={})
    if record.get('pending'):raise RuntimeError('Uncertain lab queue allocation; inspect before retry')
    def query(port,operation=0):
        result=execute(RECIPE.replace('@PORT@',str(port)).replace('@OP@',str(operation)),call)
        rows=[re.fullmatch(r'FFN_FABRIC_STATE port=(\d+) queues=(\d+) bundles=(\d+) header=(\d+) rv=0',s)
              for s in result.get('markers',[])]
        rows=[tuple(map(int,m.groups())) for m in rows if m]
        if len(rows)!=1 or rows[0][0]!=port:raise RuntimeError('Lab queue readback failed')
        return dict(port=port,queues=rows[0][1],header=rows[0][3],result=result)
    observed={p:query(p) for p in (24,3,7,8,16)}
    if observed[24]['queues']!=8 or any(r['header']!=11 or r['queues'] not in (0,8) for r in observed.values()):
        raise RuntimeError('Existing trunk and compatible queue geometry required')
    for port in (3,7,8,16):
        if observed[port]['queues']==8:continue
        record['pending']=port;save(record,path)
        after=query(port,1)
        if after['queues']!=8:raise RuntimeError('Lab queue preparation unverified')
        record['ports'][str(port)]=after;record.pop('pending');save(record,path)
    return dict(completed=True,markers=['FFN_DONE'],queues={str(p):query(p) for p in observed})


def queue_ids(call):
    source='''
int ffn_lab_queues(int unit,int port,int numq,uint32 flags,int gport,void *data) {
 int rv;int qid;
 if(BCM_GPORT_IS_UCAST_QUEUE_GROUP(gport)) {
  rv=bcm_cosq_gport_get(unit,gport,&port,&numq,&flags);if(rv)return rv;
  port=port & 0x7ff;
  if(port==3 || port==7 || port==8 || port==16 || port==24) {
   qid=BCM_GPORT_UCAST_QUEUE_GROUP_QID_GET(gport);
   printf("FFN_LAB_QUEUE port=%d qid=%d count=%d\\n",port,qid,numq);
  }
 } return 0;
}
{int rv=bcm_cosq_gport_traverse(0,ffn_lab_queues,NULL);if(rv==0)printf("FFN_DONE\\n");}
'''
    result=execute(source,call);queues={}
    for line in result['markers']:
        m=re.fullmatch(r'FFN_LAB_QUEUE port=(\d+) qid=(\d+) count=8',line)
        if not m:continue
        port,qid=map(int,m.groups())
        if port in queues or not 0<=qid<=65535:raise RuntimeError('Ambiguous or unsupported lab queue')
        queues[port]=qid
    if set(queues)!={3,7,8,16,24}:raise RuntimeError('Missing lab queues')
    return dict(completed=True,markers=['FFN_DONE'],queue_ids=queues)


def baseline(mode,call,epoch):
    def get(port):return read_port(execute(port_recipe(port),call),port)
    def set_port(row):
        actual=read_port(execute(port_recipe(row['port'],row['destination'],row['enabled']),call),row['port'])
        if actual['enabled']!=row['enabled'] or row['enabled'] and actual['destination']!=row['destination']:
            raise RuntimeError('Lab route restoration/activation mismatch')
    if mode=='baseline-begin':
        if STATE.exists() and json.loads(STATE.read_text()).get('stage')!='restored':
            raise RuntimeError('Pending lab route cleanup must be completed first')
        rows=[get(p) for p in (16,7)]
        if any(r['enabled'] and r['destination']!=24 for r in rows):raise RuntimeError('Unexpected isolated loop owner')
        record=dict(epoch=epoch,stage='preparing',ports=rows);save(record)
        for row in rows:set_port(dict(row,destination=24,enabled=1))
        record['stage']='active';save(record)
    else:
        if not STATE.exists():return dict(completed=True,markers=['FFN_DONE'],restored=True)
        record=json.loads(STATE.read_text())
        if record['stage']=='restored':return dict(completed=True,markers=['FFN_DONE'],restored=True)
        if record['epoch']!=epoch:raise RuntimeError('BCM lifetime changed; stale cleanup refused')
        for row in record['ports']:
            current=get(row['port'])
            if current['enabled'] and current['destination']!=24:raise RuntimeError('Lab redirect still active or owner changed')
        for row in record['ports']:set_port(row)
        record['stage']='restored';save(record)
    return dict(completed=True,markers=['FFN_DONE'],baseline=record,restored=record['stage']=='restored')


def run(request):
    from ffn_faceplate import call
    from ffn_copper_forwarding import epoch
    mode=request['mode']
    with locked(Path('/run/ffn-fe100-bcm-lab.lock')):
        if mode in ('baseline-begin','baseline-end'):return baseline(mode,call,epoch())
        if mode=='queues-prepare':return prepare_queues(call,epoch())
        if mode=='queue-status':return queue_ids(call)
        return execute(render(mode,request.get('ids',{})),call)


if __name__=='__main__':
    try:print(json.dumps(run(json.load(sys.stdin))))
    except (ValueError,OSError,RuntimeError,KeyError) as e:
        print(json.dumps({'completed':False,'error':str(e)}));raise SystemExit(2)
