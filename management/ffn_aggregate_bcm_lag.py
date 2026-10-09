#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Owned Jericho egress LAG with fixed source aggregate member indices, and the
ingress load-balance key program that lets the chip spread flows across it.

Injected frames enter the switch on the TM-header trunk port, where the
pipeline parses no packet header, so the LAG load-balance key it resolves the
destination with would be one constant and every flow would leave through one
member. The DP carries an 8-bit flow key in the RAW_DSA area of every
hardware-egress frame (octeon/native/ffn_aggregate_packet.inc); a direct-
extraction field entry preselected on that port copies the byte into the LAG
LB key before the destination is resolved. The switch strips the area before
faceplate transmission. The program is one shared resource for every offloaded
aggregate: installed and verified before the first owned trunk is created,
verified with every readback, removed after the last owned trunk is destroyed.
"""
import re

LB_KEY='pmf-dsa'
# Fixed identities, so a readback proves exactly this program is present.
LBK_PORT=24       # the OCTEON trunk port: tm_port_header_type_in_24=TM
LBK_GROUP=12
LBK_PRESEL=12
LBK_QUAL=12
LBK_PRIORITY=12
# Bit offset from the field processor's packet start of a RAW_DSA key byte.
# The DP writes the key into bytes 1..3 of the area (byte 0 is the DSA tagged
# flag, bytes 4..7 are PCP/VID; the egress program reads those), so the offset
# must land in bytes 1..3: 104 = byte 13 counted from the Ethernet DA, i.e. the
# packet start does not include the four-byte ITMH (136 would if it did).
LBK_OFFSET=104
LBK_BITS=8        # key width; the DP folds its flow hash to this many low bits

# The caller holds the aggregate ownership lock and journals before mutation.
# No global hash settings, port headers, queue allocations or VLANs are changed.
RECIPE=r'''
int ffn_lbk_status(int u,int group,int qual,int *entries,int *ok) {
 bcm_field_group_status_t st; bcm_field_entry_t ents[4]; int n=0; int cnt=0; int rv;
 bcm_field_extraction_action_t act; bcm_field_extraction_field_t ext[4];
 *entries=0;*ok=0;
 rv=bcm_field_group_status_get(u,group,&st); if(rv!=0)return rv;
 *entries=st.entry_count;
 rv=bcm_field_entry_multi_get(u,group,4,ents,&n); if(rv!=0)return rv;
 if(n!=1)return 0;
 /* The readback is by action: the query names the action it reads. */
 bcm_field_extraction_action_t_init(&act);act.action=bcmFieldActionTrunkHashKeySet;
 rv=bcm_field_direct_extraction_action_get(u,ents[0],&act,4,ext,&cnt); if(rv!=0)return rv;
 /* The SDK pads the action to its width with constant-zero fields after the
  * eight data bits; the key is the low byte, nothing else may be extracted. */
 if(act.action==bcmFieldActionTrunkHashKeySet && cnt>=1 && ext[0].bits==@LBBITS@ && ext[0].lsb==0 && ext[0].qualifier==qual && (ext[0].flags & BCM_FIELD_EXTRACTION_FLAG_DATA_FIELD)) {
  *ok=1;
  for(n=1;n<cnt && n<4;n++) { if(!(ext[n].flags & BCM_FIELD_EXTRACTION_FLAG_CONSTANT) || ext[n].value!=0)*ok=0; }
 }
 return 0;
}
int ffn_lbk_install(int u,int port,int group,int presel,int qual,int offset,int prio) {
 int rv; bcm_field_presel_set_t psset; bcm_pbmp_t pd; bcm_pbmp_t pm; bcm_field_group_config_t g;
 bcm_field_data_qualifier_t q; bcm_field_entry_t e; bcm_field_extraction_field_t ext[1]; bcm_field_extraction_action_t act;
 BCM_FIELD_PRESEL_INIT(psset);
 rv=bcm_field_presel_create_id(u,presel); if(rv!=0)return rv;
 rv=bcm_field_qualify_Stage(u,presel|BCM_FIELD_QUALIFY_PRESEL,bcmFieldStageIngress); if(rv!=0)return rv;
 BCM_PBMP_CLEAR(pd);BCM_PBMP_NEGATE(pm,pd);BCM_PBMP_PORT_ADD(pd,port);
 rv=bcm_field_qualify_InterfaceInPorts(u,presel|BCM_FIELD_QUALIFY_PRESEL,pd,pm); if(rv!=0)return rv;
 BCM_FIELD_PRESEL_ADD(psset,presel);
 bcm_field_data_qualifier_t_init(&q);
 q.qual_id=qual;q.flags=BCM_FIELD_DATA_QUALIFIER_WITH_ID|BCM_FIELD_DATA_QUALIFIER_OFFSET_BIT_RES|BCM_FIELD_DATA_QUALIFIER_LENGTH_BIT_RES;
 q.offset_base=bcmFieldDataOffsetBasePacketStart;q.offset=offset;q.length=@LBBITS@;q.stage=bcmFieldStageIngress;
 rv=bcm_field_data_qualifier_create(u,&q); if(rv!=0)return rv;
 bcm_field_group_config_t_init(&g);g.group=group;
 BCM_FIELD_QSET_INIT(g.qset);BCM_FIELD_QSET_ADD(g.qset,bcmFieldQualifyStageIngress);
 rv=bcm_field_qset_data_qualifier_add(u,&g.qset,qual); if(rv!=0)return rv;
 BCM_FIELD_ASET_INIT(g.aset);BCM_FIELD_ASET_ADD(g.aset,bcmFieldActionTrunkHashKeySet);
 g.priority=prio;g.mode=bcmFieldGroupModeDirectExtraction;g.preselset=psset;
 g.flags=BCM_FIELD_GROUP_CREATE_WITH_ID|BCM_FIELD_GROUP_CREATE_WITH_MODE|BCM_FIELD_GROUP_CREATE_WITH_ASET|BCM_FIELD_GROUP_CREATE_WITH_PRESELSET;
 rv=bcm_field_group_config_create(u,&g); if(rv!=0)return rv;
 rv=bcm_field_entry_create(u,group,&e); if(rv!=0)return rv;
 bcm_field_extraction_action_t_init(&act);act.action=bcmFieldActionTrunkHashKeySet;
 bcm_field_extraction_field_t_init(&ext[0]);ext[0].flags=BCM_FIELD_EXTRACTION_FLAG_DATA_FIELD;ext[0].bits=@LBBITS@;ext[0].lsb=0;ext[0].qualifier=qual;
 rv=bcm_field_direct_extraction_action_add(u,e,act,1,ext); if(rv!=0)return rv;
 return bcm_field_group_install(u,group);
}
int ffn_lbk_remove(int u,int group,int presel,int qual) {
 bcm_field_entry_t ents[4]; int n=0; int i; int rv;
 rv=bcm_field_entry_multi_get(u,group,4,ents,&n);
 if(rv==0) { for(i=0;i<n;i++) { rv=bcm_field_entry_destroy(u,ents[i]); if(rv!=0)return rv; } rv=bcm_field_group_destroy(u,group); }
 if(rv!=0 && rv!=-7)return rv;
 rv=bcm_field_data_qualifier_destroy(u,qual); if(rv!=0 && rv!=-7)return rv;
 rv=bcm_field_presel_destroy(u,presel); if(rv!=0 && rv!=-7)return rv;
 return 0;
}
{
 int tid=@TID@; int op=@OP@; int rv; int n=0; int p; int gp; int oldn=@OLDN@; int newn=@NEWN@;
 int old[8]={@OLD@}; int next[8]={@NEW@};
 int lbrv=0; int lbe=0; int lbok=0; int others=0; int t;
 bcm_trunk_info_t info; bcm_trunk_member_t members[8]; bcm_trunk_chip_info_t chip; bcm_field_group_status_t gst;
 rv=bcm_trunk_chip_info_get(0,&chip);
 if(rv==0 && (chip.trunk_group_count!=256 || chip.trunk_id_min!=0 || chip.trunk_id_max!=255))rv=-15;
 if(rv==0)rv=bcm_trunk_get(0,tid,&info,8,members,&n);
 if(op==1) {
  if(rv!=-7)rv=-8;
  else {
   /* The shared key program precedes the first owned trunk, so a failure
    * here leaves no trunk behind; a present program is verified, not adopted. */
   lbrv=bcm_field_group_status_get(0,@LBGROUP@,&gst);
   if(lbrv==-7)lbrv=ffn_lbk_install(0,@LBPORT@,@LBGROUP@,@LBPRESEL@,@LBQUAL@,@LBOFFSET@,@LBPRIO@);
   if(lbrv==0)lbrv=ffn_lbk_status(0,@LBGROUP@,@LBQUAL@,&lbe,&lbok);
   if(lbrv==0 && (lbe!=1 || lbok!=1))lbrv=-15;
   rv=lbrv;
   if(rv==0) { rv=bcm_trunk_create(0,BCM_TRUNK_FLAG_WITH_ID,&tid);
    if(rv==0) { bcm_trunk_info_t_init(&info);info.psc=BCM_TRUNK_PSC_PORTFLOW;rv=bcm_trunk_set(0,tid,&info,0,members); }
   }
  }
 } else if(op && rv==0) {
  if(n!=oldn || info.psc!=BCM_TRUNK_PSC_PORTFLOW)rv=-8;
  for(p=0;p<n && rv==0;p++) {
   rv=bcm_stk_gport_sysport_get(0,members[p].gport,&gp);
   if(rv==0 && (BCM_GPORT_SYSTEM_PORT_ID_GET(gp)!=old[p] || members[p].flags!=0))rv=-8;
  }
  if(rv==0 && op==3) {
   rv=bcm_trunk_destroy(0,tid);
   if(rv==0) {
    /* The program outlives this trunk while another owned trunk exists. */
    for(t=1;t<=12;t++) { if(t!=tid && bcm_trunk_get(0,t,&info,8,members,&n)==0)others=others+1; }
    if(others==0)lbrv=ffn_lbk_remove(0,@LBGROUP@,@LBPRESEL@,@LBQUAL@);
   }
  }
  if(rv==0 && op==2) {
   bcm_trunk_info_t_init(&info);info.psc=BCM_TRUNK_PSC_PORTFLOW;
   for(p=0;p<newn;p++) { bcm_trunk_member_t_init(&members[p]);BCM_GPORT_SYSTEM_PORT_ID_SET(members[p].gport,next[p]);members[p].flags=0; }
   rv=bcm_trunk_set(0,tid,&info,newn,members);
  }
 }
 printf("FFN_AE_LAG_OP rv=%d\n",rv);
 if(op==3)printf("FFN_AE_LBKEY_REMOVE others=%d rv=%d\n",others,lbrv);
 if(rv==0 || (op==0 && rv==-7)) {
  rv=bcm_trunk_get(0,tid,&info,8,members,&n);
  printf("FFN_AE_LAG tid=%d count=%d psc=%d rv=%d\n",tid,n,info.psc,rv);
  for(p=0;rv==0 && p<n && p<8;p++) {
   rv=bcm_stk_gport_sysport_get(0,members[p].gport,&gp);
   printf("FFN_AE_LAG_MEMBER port=%d flags=%d rv=%d\n",BCM_GPORT_SYSTEM_PORT_ID_GET(gp),members[p].flags,rv);
  }
  if(op!=3) {
   lbrv=ffn_lbk_status(0,@LBGROUP@,@LBQUAL@,&lbe,&lbok);
   printf("FFN_AE_LBKEY group=%d entries=%d ok=%d offset=%d rv=%d\n",@LBGROUP@,lbe,lbok,@LBOFFSET@,lbrv);
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
            'OLD':','.join(str(PORTS[p]) for p in old) or '0','NEW':','.join(str(PORTS[p]) for p in members) or '0',
            'LBPORT':LBK_PORT,'LBGROUP':LBK_GROUP,'LBPRESEL':LBK_PRESEL,'LBQUAL':LBK_QUAL,'LBOFFSET':LBK_OFFSET,'LBPRIO':LBK_PRIORITY,'LBBITS':LBK_BITS}
    script=RECIPE
    for key,value in values.items():script=script.replace('@'+key+'@',str(value))
    with open('/run/ffn-forward-test.lock','a') as lock:
        acquire(lock);previous=SCRIPT.read_bytes()
        try:
            SCRIPT.write_text(script)
            result=call({'op':'cint.run','script':SCRIPT.name,'timeout':10})
        finally:SCRIPT.write_bytes(previous)
    return parse(result,tid,operation,members)


def key_program(lines):
    """The verified load-balance key program, or None when it is absent or wrong."""
    rows=[re.fullmatch(r'FFN_AE_LBKEY group=(\d+) entries=(\d+) ok=([01]) offset=(\d+) rv=(-?\d+)',s) for s in lines];rows=[m for m in rows if m]
    if len(rows)!=1:return None
    group,entries,ok,offset,rv=map(int,rows[0].groups())
    if (group,entries,ok,offset,rv)!=(LBK_GROUP,1,1,LBK_OFFSET,0):return None
    return dict(program=LB_KEY,port=LBK_PORT,group=group,offset=offset,bits=LBK_BITS)


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
    if rv==-7 and operation in ('read','destroy'):
        value=dict(tid=tid,exists=False,members=[])
        if operation=='destroy':
            removed=[re.fullmatch(r'FFN_AE_LBKEY_REMOVE others=(\d+) rv=(-?\d+)',s) for s in lines];removed=[m for m in removed if m]
            if len(removed)!=1:raise RuntimeError('Trunk removal readback incomplete')
            # A program left behind by a failed removal is inert without a LAG
            # destination and is verified, never adopted blindly, by the next create.
            value.update(key_program_retained=int(removed[0][1])!=0 or int(removed[0][2])!=0)
        return value
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
    program=key_program(lines)
    if operation=='create' and program is None:raise RuntimeError('LAG load-balance key program not verified; hardware egress refused')
    value=dict(tid=tid,exists=True,members=found,psc=psc,ingress_metadata='physical-or-fixed-spa')
    # The DP uses hardware egress only while the key program is verified; an
    # unverified readback withholds lb_key and the DP keeps selecting in software.
    if program is not None:value.update(lb_key=LB_KEY,key_program=program)
    return value
