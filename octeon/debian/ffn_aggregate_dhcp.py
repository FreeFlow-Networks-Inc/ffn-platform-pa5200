#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""udhcpc hook scoped to an MP-owned aggregate. Never changes host DNS."""
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
sys.path.insert(0,'/usr/local/lib/ffn')
from ffn_aggregate_runtime import ip,atomic,boot,NS
from ffn_interface_management import apply
RUNDIR=Path('/run')


def lease_values(env):
    address=ipaddress.IPv4Interface(env['ip']+'/'+env['subnet'])
    if address.ip.is_unspecified or address.ip.is_multicast:raise ValueError('Invalid DHCP address')
    routers=[ipaddress.IPv4Address(v) for v in env.get('router','').split()]
    if any(r.is_unspecified or r.is_multicast or r not in address.network for r in routers):raise ValueError('Invalid DHCP router')
    return str(address),str(routers[0]) if routers else None


def execute(action,env):
    name=env.get('interface','')
    if not re.fullmatch(r'ae(?:[1-9]|1[0-2])',name):raise ValueError('Invalid DHCP interface')
    intent=json.loads((RUNDIR/('ffn-aggregate-'+name+'-intent.json')).read_text())
    if intent['boot_id']!=boot() or intent['control_only'] or not intent['network']['dhcp']:raise ValueError('No active aggregate DHCP intent')
    path=RUNDIR/('ffn-aggregate-'+name+'-lease.json');cfg=intent['network']
    with (RUNDIR/'ffn-network.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        current=json.loads((RUNDIR/('ffn-aggregate-'+name+'-intent.json')).read_text())
        if current.get('token')!=intent['token'] or current.get('network_generation')!=intent.get('network_generation'):
            raise ValueError('DHCP configuration changed while waiting for the network lock')
        if current.get('network_generation') is not None and env.get('FFN_AGGREGATE_NETWORK_REVISION')!=current['network_generation']:
            raise ValueError('Stale aggregate DHCP client generation')
        link=json.loads(ip('-j','link','show','dev',name))[0]
        if link.get('ifalias')!='ffn-aggregate:'+intent['token']:raise ValueError('DHCP netdevice ownership changed')
        previous=json.loads(path.read_text()) if path.exists() else {}
        if previous and previous.get('token')!=intent['token']:raise ValueError('Stale DHCP lease requires cleanup')
        if action not in ('deconfig','bound','renew','nak','leasefail'):raise ValueError('Unknown DHCP event')
        if action=='leasefail':return
        address,router=lease_values(env) if action in ('bound','renew') else (None,None)
        route=router if cfg['dhcp_default_route'] else None
        atomic(path,dict(previous,token=intent['token'],error='DHCP lease update pending'))
        # Delete only the previous lease's exact route/address, never flush or
        # replace the WAN/default route belonging to another interface.
        if previous.get('router') and previous['router']!=route:
            ip('route','del','default','via',previous['router'],'dev',name,'proto','186','metric',str(cfg['dhcp_route_metric']))
        if previous.get('address') and previous['address']!=address:ip('address','del',previous['address'],'dev',name)
        settings=dict(mode='l3',addresses=[address] if address else [],management=cfg['management'])
        apply(NS,name,settings)
        if address and address!=previous.get('address'):ip('address','add',address,'dev',name)
        # Journal the address before adding a route so failed route installation
        # still leaves an owned lease that can be withdrawn safely.
        value=dict(token=intent['token'],address=address,router=None,error=None)
        atomic(path,value)
        try:
            if route and route!=previous.get('router'):ip('route','add','default','via',route,'dev',name,'proto','186','metric',str(cfg['dhcp_route_metric']))
            value['router']=route
        except Exception as error:
            value['error']=str(error);atomic(path,value);raise
        atomic(path,value)


if __name__=='__main__':execute(sys.argv[1],os.environ)
