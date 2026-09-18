#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Owned Jericho egress LAG with fixed source aggregate member indices."""
import re

# The caller holds the aggregate ownership lock and journals before mutation.
# No global hash settings, port headers, queue allocations or VLANs are changed.
RECIPE=r'''
{
 int tid=@TID@; int op=@OP@; int rv; int n=0; int p; int gp; int oldn=@OLDN@; int newn=@NEWN@;
 int old[8]={@OLD@}; int next[8]={@NEW@};
 bcm_trunk_info_t info; bcm_trunk_member_t members[8]; bcm_trunk_chip_info_t chip;
 rv=bcm_trunk_chip_info_get(0,&chip);
 if(rv==0 && (chip.trunk_group_count!=256 || chip.trunk_id_min!=0 || chip.trunk_id_max!=255))rv=-15;
 if(rv==0)rv=bcm_trunk_get(0,tid,&info,8,members,&n);
 if(op==1) {
  if(rv!=-7)rv=-8;
  else { rv=bcm_trunk_create(0,BCM_TRUNK_FLAG_WITH_ID,&tid);
   if(rv==0) { bcm_trunk_info_t_init(&info);info.psc=BCM_TRUNK_PSC_PORTFLOW;rv=bcm_trunk_set(0,tid,&info,0,members); }
  }
 } else if(op && rv==0) {
  if(n!=oldn || info.psc!=BCM_TRUNK_PSC_PORTFLOW)rv=-8;
  for(p=0;p<n && rv==0;p++) {
   rv=bcm_stk_gport_sysport_get(0,members[p].gport,&gp);
   if(rv==0 && (BCM_GPORT_SYSTEM_PORT_ID_GET(gp)!=old[p] || members[p].flags!=0))rv=-8;
  }
  if(rv==0 && op==3)rv=bcm_trunk_destroy(0,tid);
  if(rv==0 && op==2) {
   bcm_trunk_info_t_init(&info);info.psc=BCM_TRUNK_PSC_PORTFLOW;
   for(p=0;p<newn;p++) { bcm_trunk_member_t_init(&members[p]);BCM_GPORT_SYSTEM_PORT_ID_SET(members[p].gport,next[p]);members[p].flags=0; }
   rv=bcm_trunk_set(0,tid,&info,newn,members);
  }
 }
 printf("FFN_AE_LAG_OP rv=%d\n",rv);
 if(rv==0 || (op==0 && rv==-7)) {
  rv=bcm_trunk_get(0,tid,&info,8,members,&n);
  printf("FFN_AE_LAG tid=%d count=%d psc=%d rv=%d\n",tid,n,info.psc,rv);
  for(p=0;rv==0 && p<n && p<8;p++) {
   rv=bcm_stk_gport_sysport_get(0,members[p].gport,&gp);
   printf("FFN_AE_LAG_MEMBER port=%d flags=%d rv=%d\n",BCM_GPORT_SYSTEM_PORT_ID_GET(gp),members[p].flags,rv);
  }
 }
 printf("FFN_AE_DONE\n");
}
'''


def trunk(tid,operation='read',old=(),members=()):
    from ffn_aggregate_hardware import PORTS,SCRIPT,acquire,call
    if type(tid) is not int or not 1<=tid<=12:raise ValueError('Invalid owned trunk ID')
    modes={'read':0,'create':1,'set':2,'destroy':3}
    if operation not in modes:raise ValueError('Invalid trunk operation')
    for ports in (old,members):
        if len(ports)>8 or len(set(ports))!=len(ports) or any(type(p) is not int or p not in PORTS for p in ports):raise ValueError('Invalid trunk members')
    values={'TID':tid,'OP':modes[operation],'OLDN':len(old),'NEWN':len(members),
            'OLD':','.join(str(PORTS[p]) for p in old) or '0','NEW':','.join(str(PORTS[p]) for p in members) or '0'}
    script=RECIPE
    for key,value in values.items():script=script.replace('@'+key+'@',str(value))
    with open('/run/ffn-forward-test.lock','a') as lock:
        acquire(lock);previous=SCRIPT.read_bytes()
        try:
            SCRIPT.write_text(script)
            result=call({'op':'cint.run','script':SCRIPT.name,'timeout':10})
        finally:SCRIPT.write_bytes(previous)
    return parse(result,tid,operation,members)


def parse(result,tid,operation,members):
    if not result.get('ok') or not result.get('completed') or result.get('truncated'):raise RuntimeError('Trunk SDK operation incomplete')
    lines=result.get('markers',[])
    if lines[-1:]!=['FFN_AE_DONE']:raise RuntimeError('Trunk readback incomplete')
    ops=[re.fullmatch(r'FFN_AE_LAG_OP rv=(-?\d+)',s) for s in lines];ops=[m for m in ops if m]
    rows=[re.fullmatch(r'FFN_AE_LAG tid=(\d+) count=(\d+) psc=(-?\d+) rv=(-?\d+)',s) for s in lines];rows=[m for m in rows if m]
    if len(ops)!=1 or int(ops[0][1]) not in ((0,-7) if operation=='read' else (0,)) or len(rows)!=1:
        raise RuntimeError('Trunk SDK mutation failed; recovery required')
    number,count,psc,rv=map(int,rows[0].groups())
    if number!=tid:raise RuntimeError('Trunk readback identity mismatch')
    if rv==-7 and operation in ('read','destroy'):return dict(tid=tid,exists=False,members=[])
    if rv!=0 or psc!=9:raise RuntimeError('Trunk mode readback mismatch')
    from ffn_aggregate_hardware import PORTS
    reverse={v:k for k,v in PORTS.items()};found=[]
    for line in lines:
        match=re.fullmatch(r'FFN_AE_LAG_MEMBER port=(\d+) flags=(\d+) rv=0',line)
        if match:
            port,flags=map(int,match.groups())
            if port not in reverse or flags!=0:raise RuntimeError('Unowned trunk member or disabled hardware distributor')
            found.append(reverse[port])
    if len(found)!=count or len(set(found))!=count:raise RuntimeError('Trunk member readback incomplete')
    if operation in ('set','create') and found!=list(members):raise RuntimeError('Trunk membership readback mismatch')
    if operation=='destroy':raise RuntimeError('Trunk removal readback mismatch')
    return dict(tid=tid,exists=True,members=found,psc=psc,ingress_metadata='physical-or-fixed-spa')
