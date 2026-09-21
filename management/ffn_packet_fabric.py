#!/usr/bin/env python3
"""Journaled BCM queue preparation for the PA-5200 packet transport.

The board's internal trunk and SDK queue geometry are hardware constants.
Front destinations come exclusively from the MP's committed interface intent.
No L2 forwarding, addresses, VLANs, link speeds or policy are invented here.
"""
import json
from pathlib import Path
import re

STATE=Path('/etc/ffn/packet-fabric.json')
LOCK=Path('/run/ffn-packet-fabric.lock')
TRUNK=24
QUEUES=8
COPPER_PORTS={1:28,2:13,3:14,4:15}

RECIPE=r'''
int ffn_fabric_inventory(int unit,int port,int numq,uint32 flags,int gport,void *data) {
 int rv;int *q=data;
 if(BCM_GPORT_IS_UCAST_QUEUE_GROUP(gport)) {
  rv=bcm_cosq_gport_get(unit,gport,&port,&numq,&flags);if(rv)return rv;
  q[37]=q[37]+1;port=port & 0x7ff;if(port<37)q[port]=q[port]+numq;
 } return 0;
}
{
 int rv=0;int q[38]={0};int p=@PORT@;int op=@OP@;int header=0;
 int modid;int e2e;int modport;int tmport;int destination_module;int sysport;int connector=0;int voq=0;int cos;
 uint32 flags;
 bcm_port_interface_info_t intf;
 bcm_port_mapping_info_t mapping;
 bcm_cosq_voq_connector_gport_t cfg;
 bcm_cosq_ingress_queue_bundle_gport_config_t ingress;
 bcm_cosq_gport_connection_t connection;
 rv=bcm_cosq_gport_traverse(0,ffn_fabric_inventory,q);
 if(rv==0)rv=bcm_switch_control_port_get(0,24,bcmSwitchPortHeaderType,&header);
 if(op==1 && rv==0 && q[p]!=0)rv=-8;
 if(op==1 && rv==0)rv=bcm_stk_modid_get(0,&modid);
 if(op==1 && rv==0)rv=bcm_port_get(0,p,&flags,&intf,&mapping);
 if(op==1 && rv==0) {
  BCM_COSQ_GPORT_E2E_PORT_SET(e2e,p);
  tmport=mapping.tm_port;destination_module=modid+mapping.core;
  BCM_GPORT_MODPORT_SET(modport,destination_module,tmport);
  rv=bcm_stk_gport_sysport_get(0,modport,&sysport);
  cfg.flags=BCM_COSQ_GPORT_VOQ_CONNECTOR;cfg.port=e2e;cfg.numq=8;
  cfg.remote_modid=modid;cfg.nof_remote_cores=2;
  if(rv==0)rv=bcm_cosq_voq_connector_gport_add(0,&cfg,&connector);
  printf("FFN_FABRIC_CONNECTOR port=%d id=0x%x rv=%d\n",p,connector,rv);
  for(cos=0;cos<8 && rv==0;cos++) {
   rv=bcm_cosq_gport_sched_set(0,connector,cos,BCM_COSQ_SP0,0);
   if(rv==0)rv=bcm_cosq_gport_attach(0,e2e,connector,cos);
  }
  ingress.flags=BCM_COSQ_GPORT_UCAST_QUEUE_GROUP;ingress.port=modport;
  ingress.local_core_id=BCM_CORE_ALL;ingress.numq=8;
  for(cos=0;cos<8;cos++) {
   ingress.queue_atrributes[cos].delay_tolerance_level=BCM_COSQ_DELAY_TOLERANCE_10G_SLOW_ENABLED;
   ingress.queue_atrributes[cos].rate_class=0;
  }
  if(rv==0)rv=bcm_cosq_ingress_queue_bundle_gport_add(0,&ingress,&voq);
  printf("FFN_FABRIC_VOQ port=%d id=0x%x rv=%d\n",p,voq,rv);
  connection.flags=BCM_COSQ_GPORT_CONNECTION_INGRESS;connection.remote_modid=modid+mapping.core;
  connection.voq=voq;connection.voq_connector=connector;
  if(rv==0)rv=bcm_cosq_gport_connection_set(0,&connection);
  connection.flags=BCM_COSQ_GPORT_CONNECTION_EGRESS;connection.remote_modid=modid;
  if(rv==0)rv=bcm_cosq_gport_connection_set(0,&connection);
 }
 if(op==2 && rv==0 && header!=BCM_SWITCH_PORT_HEADER_TYPE_TM_SSP) {
  if(q[37]!=0 || header!=BCM_SWITCH_PORT_HEADER_TYPE_ETH)rv=-8;
  if(rv==0)rv=bcm_switch_control_port_set(0,24,bcmSwitchPortHeaderType,BCM_SWITCH_PORT_HEADER_TYPE_TM);
  if(rv==0)rv=bcm_switch_control_port_set(0,24,bcmSwitchPortHeaderType,BCM_SWITCH_PORT_HEADER_TYPE_TM_SSP);
 }
 if(rv==0) {
  for(cos=0;cos<38;cos++)q[cos]=0;
  rv=bcm_cosq_gport_traverse(0,ffn_fabric_inventory,q);
 }
 if(rv==0)rv=bcm_switch_control_port_get(0,24,bcmSwitchPortHeaderType,&header);
 printf("FFN_FABRIC_STATE port=%d queues=%d bundles=%d header=%d rv=%d\n",p,q[p],q[37],header,rv);
 if(rv==0)printf("FFN_FABRIC_DONE\n");
}
'''


def query(port,operation=0):
    from ffn_aggregate_hardware import PORTS,LOCK,acquire,SCRIPT,call
    if port not in (*PORTS.values(),*COPPER_PORTS.values(),TRUNK) or operation not in (0,1,2):raise ValueError('Invalid fabric operation')
    with open('/run/ffn-forward-test.lock','a') as lock:
        acquire(lock)
        previous=SCRIPT.read_bytes()
        try:
            SCRIPT.write_text(RECIPE.replace('@PORT@',str(port)).replace('@OP@',str(operation)))
            result=call({'op':'cint.run','script':SCRIPT.name,'timeout':30})
        finally:SCRIPT.write_bytes(previous)
    if operation:
        from ffn_aggregate_hardware import atomic
        atomic(STATE.with_name('packet-fabric-last-result.json'),dict(port=port,operation=operation,result=result))
    if not result.get('completed') or result.get('truncated'):raise RuntimeError('Packet fabric operation incomplete; inspect allocation journal and last-result report')
    rows=[re.fullmatch(r'FFN_FABRIC_STATE port=(\d+) queues=(\d+) bundles=(\d+) header=(\d+) rv=0',line) for line in result.get('markers',[])]
    rows=[r for r in rows if r]
    if len(rows)!=1 or result['markers'][-1:]!=['FFN_FABRIC_DONE']:raise RuntimeError('Invalid packet fabric readback')
    value=dict(zip(('port','queues','bundles','header'),map(int,rows[0].groups())))
    if value['port']!=port:raise RuntimeError('Packet fabric port mismatch')
    value['allocations']=[line for line in result['markers'] if line.startswith(('FFN_FABRIC_CONNECTOR','FFN_FABRIC_VOQ'))]
    return value


def ensure(ports,expected_epoch,read=query):
    """Caller fences its own ports and holds the faceplate lock.

    Allocation has its own journal lock: WAN preparation must not hold the
    aggregate owner lock and starve that owner's link heartbeat.
    """
    from ffn_aggregate_hardware import acquire
    with LOCK.open('a') as lock:
        acquire(lock)
        return _ensure(ports,expected_epoch,read)


def _ensure(ports,expected_epoch,read):
    from ffn_aggregate_hardware import PORTS,epoch,atomic
    PORTS=PORTS|COPPER_PORTS
    if not ports or len(set(ports))!=len(ports) or any(type(p) is not int or p not in PORTS for p in ports):raise ValueError('Invalid fabric members')
    def fence():
        if epoch()!=expected_epoch:raise RuntimeError('BCM lifetime changed during fabric preparation')
    fence()
    saved=json.loads(STATE.read_text()) if STATE.exists() else {}
    if saved.get('epoch')!=expected_epoch:saved=dict(epoch=expected_epoch,ports={})
    if saved.get('pending'):raise RuntimeError('Uncertain packet fabric allocation; automatic retry withheld')
    required=[TRUNK]+sorted(PORTS[p] for p in ports)
    observed={p:read(p) for p in required}
    if any(row['queues'] not in (0,QUEUES) for row in observed.values()):raise RuntimeError('Conflicting packet fabric queue layout')
    if observed[TRUNK]['header']!=11:
        if observed[TRUNK]['header']!=1 or observed[TRUNK]['bundles']!=0:raise RuntimeError('Cannot change an occupied packet trunk header')
        fence();saved['pending']={'operation':'header'};atomic(STATE,saved)
        after=read(TRUNK,2);fence()
        if after['header']!=11:raise RuntimeError('Packet trunk header not verified')
        saved.pop('pending');saved['header']=after['header'];atomic(STATE,saved)
    for port in required:
        fence()
        if observed[port]['queues']==QUEUES:continue
        saved['pending']={'operation':'queues','port':port};atomic(STATE,saved)
        after=read(port,1);fence()
        if after['queues']!=QUEUES:raise RuntimeError('Packet queues not verified')
        saved['ports'][str(port)]=after;saved.pop('pending');atomic(STATE,saved)
    verified={str(port):read(port) for port in required};fence()
    if any(row['queues']!=QUEUES or row['header']!=11 for row in verified.values()):raise RuntimeError('Packet fabric final readback failed')
    return dict(ready=True,epoch=expected_epoch,ports=verified,forwarding_verified=False)
