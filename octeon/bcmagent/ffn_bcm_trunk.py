"""DPP trunk operations with typed system ports and exact mutation readback.

Configuration owners must serialize their requests. Existing trunks may only
be reused when their complete membership and hash selection already match.
Destroy requires the caller's expected members, including an explicit empty
list for an empty trunk. Hardware capacity is read rather than guessed.
"""
import re


def arguments(req,require_ports=False):
    if set(req)-{'op','tid','ports'}:raise ValueError('Unknown trunk fields')
    tid=req.get('tid')
    if type(tid) is not int or not 0<=tid<=65535:raise ValueError('Integer trunk ID required')
    ports=req.get('ports')
    if require_ports or ports is not None:
        if (not isinstance(ports,list) or len(ports)>8 or
            any(type(p) is not int or not 0<=p<=255 for p in ports) or len(set(ports))!=len(ports)):
            raise ValueError('Expected unique SDK system ports required (maximum eight)')
    return tid,ports


def operation(chip,req,mode):
    tid,ports=arguments(req,mode!='read');ports=ports or []
    # A single chip.run holds the SDK lock across ownership check and mutation.
    script=r'''{
 int tid=@TID@; int mode=@MODE@; int expected=@COUNT@; int wanted[8]={@PORTS@};
 int rv; int n=0; int i; int gp; int existed=0; int created=0; int rollback=0; int checked=0;
 bcm_trunk_chip_info_t cap; bcm_trunk_info_t info; bcm_trunk_member_t members[64];
 bcm_trunk_info_t_init(&info);
 rv=bcm_trunk_chip_info_get(0,&cap);
 if(rv==0 && (tid<cap.trunk_id_min || tid>cap.trunk_id_max || expected>cap.trunk_ports_max))rv=-4;
 if(rv==0) { checked=1;rv=bcm_trunk_get(0,tid,&info,64,members,&n); }
 if(rv==0)existed=1;
 if(mode && rv==0) {
  if(n!=expected || info.psc!=BCM_TRUNK_PSC_PORTFLOW)rv=-8;
  for(i=0;rv==0 && i<n;i++) {
   rv=bcm_stk_gport_sysport_get(0,members[i].gport,&gp);
   if(rv==0 && (BCM_GPORT_SYSTEM_PORT_ID_GET(gp)!=wanted[i] || members[i].flags!=0))rv=-8;
  }
 }
 if(mode==1 && checked && rv==-7) {
  rv=bcm_trunk_create(0,BCM_TRUNK_FLAG_WITH_ID,&tid);
  if(rv==0) {
   created=1; bcm_trunk_info_t_init(&info);info.psc=BCM_TRUNK_PSC_PORTFLOW;
   for(i=0;i<expected;i++) {bcm_trunk_member_t_init(&members[i]);BCM_GPORT_SYSTEM_PORT_ID_SET(members[i].gport,wanted[i]);}
   rv=bcm_trunk_set(0,tid,&info,expected,members);
   if(rv!=0)rollback=bcm_trunk_destroy(0,tid);
  }
 }
 if(mode==2 && rv==0)rv=bcm_trunk_destroy(0,tid);
 printf("FFN_TRUNK_OP %d %d %d %d\n",rv,existed,created,rollback);
 if(checked && (rv==0 || rv==-7)) {
  bcm_trunk_info_t_init(&info);n=0;rv=bcm_trunk_get(0,tid,&info,64,members,&n);
  printf("FFN_TRUNK %d %d %d %d\n",tid,rv,n,info.psc);
  for(i=0;rv==0 && i<n && i<64;i++) {
   rv=bcm_stk_gport_sysport_get(0,members[i].gport,&gp);
   printf("FFN_TRUNK_MEMBER %d %d %d\n",rv,BCM_GPORT_SYSTEM_PORT_ID_GET(gp),members[i].flags);
  }
 }
 printf("FFN_TRUNK_DONE\n");
}'''
    for key,value in {'TID':tid,'MODE':{'read':0,'create':1,'destroy':2}[mode],
                      'COUNT':len(ports),'PORTS':','.join(map(str,ports)) or '0'}.items():
        script=script.replace('@'+key+'@',str(value))
    output=chip.run('cint\n'+script+'\nexit;')
    lines=[line.strip() for line in output.splitlines() if line.strip().startswith('FFN_TRUNK')]
    if len(lines)<2 or lines[-1:]!=['FFN_TRUNK_DONE']:raise RuntimeError('Trunk SDK operation incomplete; reconcile ownership')
    op=re.fullmatch(r'FFN_TRUNK_OP (-?\d+) ([01]) ([01]) (-?\d+)',lines[0])
    if not op:raise RuntimeError('Missing trunk mutation acknowledgement')
    rv,existed,created,rollback=map(int,op.groups())
    if rv not in (0,-7) or rv==-7 and mode=='create':
        raise RuntimeError('Trunk SDK operation failed: rv=%d rollback=%d; ownership must be reconciled'%(rv,rollback))
    row=re.fullmatch(r'FFN_TRUNK (\d+) (-?\d+) (\d+) (-?\d+)',lines[1]) if len(lines)>2 else None
    if not row or int(row[1])!=tid:raise RuntimeError('Trunk readback identity mismatch')
    read_rv,count,psc=map(int,row.groups()[1:])
    if read_rv==-7:
        if mode=='create' or len(lines)!=3:raise RuntimeError('Created trunk is absent')
        return dict(tid=tid,exists=False,members=[],verified=True,destroyed=bool(existed),absent=True)
    if read_rv!=0 or count>64 or len(lines)!=count+3:raise RuntimeError('Incomplete trunk readback')
    actual=[];flags=[]
    for line in lines[2:-1]:
        member=re.fullmatch(r'FFN_TRUNK_MEMBER 0 (\d+) (\d+)',line)
        if not member:raise RuntimeError('Trunk member readback failed')
        actual.append(int(member[1]));flags.append(int(member[2]))
    if len(set(actual))!=count:raise RuntimeError('Duplicate hardware trunk members')
    if mode=='destroy' or mode=='create' and (actual!=ports or psc!=9 or any(flags)):
        raise RuntimeError('Trunk mutation readback mismatch')
    return dict(tid=tid,exists=True,members=actual,member_flags=flags,psc=psc,
                created=bool(created),existed=bool(existed),verified=True)


def read(chip,req):return operation(chip,req,'read')
def create(chip,req):return operation(chip,req,'create')
def destroy(chip,req):return operation(chip,req,'destroy')
