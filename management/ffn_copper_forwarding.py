#!/usr/bin/env python3
"""CP owner for the commissioned copper 3/4 -> OCTEON return paths.

No queue allocation, SDK restart, PHY changes, or WAN control. An interrupted
operation blocks readiness until explicit recovery disables the owned pair.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import sys
from ffn_faceplate import call

STATE=Path('/etc/ffn/copper-forwarding.json')
LOCK=Path('/run/ffn-copper-forwarding.lock')
QUALIFIED=Path('/etc/ffn/copper-forwarding-qualified.json')
SCRIPT=Path('/opt/ffn-compat/tmp/bcmcfg/ffn_bcm_forward_test.c')
PORTS={3:14,4:15}

RECIPE=r'''
int ffn_cf_inventory(int unit,int port,int numq,uint32 flags,int gport,void *data) {
 int rv;
 int *queues=data;
 if(BCM_GPORT_IS_UCAST_QUEUE_GROUP(gport)) {
  rv=bcm_cosq_gport_get(unit,gport,&port,&numq,&flags);
  if(rv)return rv;
  port=port & 0x7ff;
  if(port<37)queues[port]=queues[port]+numq;
 }
 return 0;
}
{
 int rv=0; int p; int dst; int enabled; int header; int mode=MODE;
 int ffn_cf_queues[37] = {0};
 rv=bcm_cosq_gport_traverse(0,ffn_cf_inventory,ffn_cf_queues);
 if(rv==0)rv=bcm_switch_control_port_get(0,24,bcmSwitchPortHeaderType,&header);
 printf("FFN_CF_HEADER value=%d queues=%d rv=%d\n",header,ffn_cf_queues[24],rv);
 if(mode==1 && rv==0 && (header!=BCM_SWITCH_PORT_HEADER_TYPE_TM_SSP || ffn_cf_queues[14]!=8 || ffn_cf_queues[15]!=8 || ffn_cf_queues[24]!=8))rv=-1;
 for(p=14;p<=15 && rv==0;p++) {
  rv=bcm_port_force_forward_get(0,p,&dst,&enabled);
  if(mode && rv==0 && enabled && dst!=24)rv=-8;
 }
 for(p=14;p<=15 && rv==0;p++) {
  if(mode)rv=bcm_port_force_forward_set(0,p,24,mode==1);
  if(rv==0)rv=bcm_port_force_forward_get(0,p,&dst,&enabled);
  printf("FFN_CF_PORT port=%d dst=%d enabled=%d queues=%d rv=%d\n",p,dst,enabled,ffn_cf_queues[p],rv);
  if(mode && rv==0 && (enabled!=(mode==1) || (enabled && dst!=24)))rv=-1;
 }
 if(rv)printf("FFN_CF_FAIL rv=%d\n",rv);else printf("FFN_CF_DONE\n");
}
'''


def epoch(root=Path('/proc')):
    owners=[]
    for p in root.iterdir():
        if not p.name.isdigit():continue
        try:
            args=(p/'cmdline').read_bytes().split(b'\0')
            # Match argv entries, never a shell command containing these names.
            if any(Path(a.decode()).name=='ffn_bcmd.py' for a in args if a):
                start=(p/'stat').read_text().rsplit(') ',1)[1].split()[19]
                owners.append(p.name+':'+start)
        except (OSError,UnicodeError,IndexError):continue
    if len(owners)!=1:raise RuntimeError('one BCM owner process required')
    return (root/'sys/kernel/random/boot_id').read_text().strip()+':'+owners[0]


def save(value):
    STATE.parent.mkdir(parents=True,exist_ok=True)
    temp=STATE.with_suffix('.tmp')
    with temp.open('w') as out:json.dump(value,out);out.flush();os.fsync(out.fileno())
    temp.replace(STATE)
    fd=os.open(STATE.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def parse(result):
    if result.get('ok') is not True or not result.get('completed') or result.get('truncated'):
        raise RuntimeError('BCM copper operation incomplete; inspect pending state')
    lines=result.get('markers',[])
    headers=[re.fullmatch(r'FFN_CF_HEADER value=(\d+) queues=(\d+) rv=0',s) for s in lines]
    headers=[m for m in headers if m]
    rows={}
    for line in lines:
        m=re.fullmatch(r'FFN_CF_PORT port=(\d+) dst=(\d+) enabled=([01]) queues=(\d+) rv=0',line)
        if m:
            port,dst,enabled,queues=map(int,m.groups())
            if port in rows:raise RuntimeError('duplicate BCM copper observation')
            rows[port]={'destination':dst,'enabled':bool(enabled),'queues':queues}
    if len(headers)!=1 or set(rows)!={14,15} or lines[-1]!='FFN_CF_DONE':
        raise RuntimeError('invalid BCM copper readback')
    return {'header':int(headers[0][1]),'trunk_queues':int(headers[0][2]),'ports':rows}


def run(mode):
    with open('/run/ffn-forward-test.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        old=SCRIPT.read_bytes() if SCRIPT.exists() else None
        try:
            SCRIPT.write_text(RECIPE.replace('MODE',str(mode)))
            return parse(call({'op':'cint.run','script':SCRIPT.name,'timeout':30}))
        finally:
            if old is None:SCRIPT.unlink(missing_ok=True)
            else:SCRIPT.write_bytes(old)


def readiness(saved,proof,current,hardware):
    qualified=(proof.get('schema')==1 and proof.get('epoch')==current and proof.get('ports')==[3,4])
    active=qualified and saved.get('epoch')==current and saved.get('enabled') is True and not saved.get('pending')
    return {str(front):bool(active and hardware['header']==11 and hardware['trunk_queues']==8
            and hardware['ports'][mac]=={'destination':24,'enabled':True,'queues':8}) for front,mac in PORTS.items()}


def execute(op):
    if op not in ('status','start','stop','recover'):raise ValueError('unsupported copper forwarding operation')
    with LOCK.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        current=epoch()
        saved=json.loads(STATE.read_text()) if STATE.exists() else {'enabled':False,'pending':None}
        proof=json.loads(QUALIFIED.read_text()) if QUALIFIED.exists() else {}
        hardware=run(0)
        if op!='status':
            if op=='start':
                if proof.get('schema')!=1 or proof.get('epoch')!=current or proof.get('ports')!=[3,4]:
                    raise RuntimeError('copper paths require qualification in this BCM lifetime')
                if saved.get('pending'):raise RuntimeError('recover interrupted copper operation first')
                if saved.get('epoch')!=current or saved.get('enabled') is not True:
                    if any(r['enabled'] for r in hardware['ports'].values()):raise RuntimeError('refusing to adopt existing copper redirects')
            elif saved.get('epoch')!=current:
                raise RuntimeError('BCM owner changed; refusing stale cleanup')
            save({'epoch':current,'enabled':saved.get('enabled',False),'pending':op})
            hardware=run(1 if op=='start' else 2)
            if epoch()!=current:raise RuntimeError('BCM owner changed during operation')
            saved={'epoch':current,'enabled':op=='start','pending':None};save(saved)
        if epoch()!=current:raise RuntimeError('BCM owner changed during observation')
        return {'epoch':current,'state':saved,'hardware':hardware,
                'ready':readiness(saved,proof,current,hardware),'scope':[3,4]}


if __name__=='__main__':
    print(json.dumps(execute(sys.argv[1] if len(sys.argv)==2 else 'status')))
