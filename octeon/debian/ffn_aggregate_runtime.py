#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""The aggregate owner's kernel-interface transactions and watchdog on the DP.

The owner itself is ffn-portd (octeon/native/ffn_portd.c, kind aggregate): the LACP
engine, the member gates, the trunk control socket and the TAP's native packet
workers live there, bound to the MP supervisor's control pipe. `serve` hands
that pipe straight to it. What stays here is the configuration work it
delegates to bounded one-shot helpers, under the shared network lock:

  validate          the full intent validation (profiles, VLAN units, addresses)
  network-setup     create the TAP, its guard, addresses and VLAN units
  network-update    apply a committed address/profile/unit change
  network-teardown  remove what setup created
  status            what the MP reads; `recover` and `sweep` (the dataplane
                    watchdog) are the daemon's own verbs and exec it
"""
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
sys.path.insert(0,'/usr/local/lib/ffn')
from ffn_aggregate_datapath import Gates
from ffn_lacp_engine import Engine
import ffn_aggregate_vlan as vlan
from ffn_dp_packet_transport import FRONT

NS='ffn-data'
DAEMON=os.environ.get('FFN_PORTD','/usr/local/sbin/ffn-portd')


def boot():return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def run(*args,**kw):
    return subprocess.run(list(args),check=True,text=True,capture_output=True,timeout=10,**kw).stdout


def ip(*args):return run('ip','-n',NS,*args)


def atomic(path,data):
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(data));temp.chmod(0o600);temp.replace(path)


def status():
    groups={}
    for path in Path('/run').glob('ffn-aggregate-ae*-status.json'):
        try:
            row=json.loads(path.read_text());pid=row['pid']
            start=Path('/proc',str(pid),'stat').read_text().rsplit(') ',1)[1].split()[19]
            row['fresh']=row['boot_id']==boot() and start==row['process_start'] and 0<=time.monotonic()-row['updated_monotonic']<3
            if not row['fresh']:row.update(distributing=[],attachment_ready=False,hardware_offload=False)
            groups[row['group']]=row
        except (OSError,ValueError,KeyError,IndexError):continue
    return dict(boot_id=boot(),groups=groups,hardware_offload=False)


def teardown(name,token,management):
    """Remove the owned aggregate interface, its units, guard and run files. Network lock held."""
    links={p['ifname']:p for p in json.loads(ip('-j','link'))}
    if name in links:
        if links[name].get('ifalias')!='ffn-aggregate:'+token:raise ValueError('Refusing to delete an unowned aggregate interface')
        ip('link','set',name,'down')
        vlan.cleanup(NS,name,token,ip)
        from ffn_interface_management import apply
        apply(NS,name,dict(mode='l3',addresses=[],management=management),remove=True)
        ip('link','delete',name)
    tables=json.loads(run('ip','netns','exec',NS,'nft','-j','list','tables'))['nftables']
    if any(t.get('table',{}).get('name')=='ffn_aggregate_'+name for t in tables):run('ip','netns','exec',NS,'nft','delete','table','inet','ffn_aggregate_'+name)
    for suffix in ('lease.json','network-pending.json','dhcp.pid'):
        Path('/run/ffn-aggregate-'+name+'-'+suffix).unlink(missing_ok=True)


def validate(intent):
    expected={'group','token','boot_id','system','members','lacp','network','lldp','control_only','offload'}
    if not isinstance(intent,dict) or set(intent)!=expected:raise ValueError('Invalid aggregate owner intent')
    if not re.fullmatch(r'ae(?:[1-9]|1[0-2])',intent['group']):raise ValueError('Invalid aggregate name')
    import uuid
    if str(uuid.UUID(intent['token']))!=intent['token'] or intent['boot_id']!=boot():raise ValueError('Fresh owner token and DP lifetime required')
    members=intent['members']
    if not isinstance(members,list) or not 2<=len(members)<=8 or any(type(p) is not int or p not in FRONT for p in members) or len(set(members))!=len(members):
        raise ValueError('Invalid optical members')
    if any(type(intent[k]) is not bool for k in ('control_only','lldp','offload')) or intent['control_only'] and intent['offload']:raise ValueError('Invalid owner flags')
    network=intent['network']
    if set(network)-{'enabled','units'}!={'addresses','dhcp','dhcp_default_route','dhcp_route_metric','mtu','management'}:raise ValueError('Invalid aggregate network settings')
    if type(network.get('enabled',True)) is not bool:raise ValueError('Invalid parent network enable')
    if not network.get('enabled',True) and (network['addresses'] or network['dhcp'] or network['management'].get('profile')):raise ValueError('Link-only parent cannot carry network settings')
    if type(network['mtu']) is not int or not 576<=network['mtu']<=1500:raise ValueError('Aggregate packet path supports MTU 576..1500')
    if any(type(network[k]) is not bool for k in ('dhcp','dhcp_default_route')):raise ValueError('Invalid DHCP settings')
    if type(network['dhcp_route_metric']) is not int or not 1<=network['dhcp_route_metric']<=65535:raise ValueError('Invalid route metric')
    if not isinstance(network['addresses'],list) or len(network['addresses'])>32:raise ValueError('Invalid addresses')
    for address in network['addresses']:ipaddress.ip_interface(address)
    if network['dhcp'] and network['addresses']:raise ValueError('Choose DHCP or static addressing')
    from ffn_interface_management import validate as validate_management
    validate_management(network['management'])
    vlan.validate(intent['group'],network)
    lacp=intent['lacp']
    if set(lacp)!={'activity','rate','min_links','system_priority'}:raise ValueError('Invalid LACP settings')
    # Engine validates all protocol parameters and the stable system MAC.
    Engine(intent['system'],int(intent['group'][2:]),{p:intent['system'] for p in members},Gates(members),**lacp)
    return intent


def network_revision(intent):
    return hashlib.sha256(json.dumps({key:intent[key] for key in ('network','lldp')},sort_keys=True).encode()).hexdigest()


def network_setup(request):
    """Create the aggregate TAP and everything attached to it. Undone on failure."""
    intent=validate(request['intent']);name=intent['group'];network=intent['network'];token=intent['token']
    created=guarded=False
    with open('/run/ffn-network.lock','a') as lock:
        # This is a shared configuration transaction lock, not aggregate
        # ownership. Wait for a policy apply instead of failing the owner on
        # ordinary contention; the owner bounds the wait.
        fcntl.flock(lock,fcntl.LOCK_EX)
        try:
            if any(n['ifname']==name for n in json.loads(ip('-j','link'))):raise ValueError('Aggregate netdevice already exists; refusing adoption')
            ip('tuntap','add','dev',name,'mode','tap','multi_queue');created=True
            ip('link','set',name,'alias','ffn-aggregate:'+token)
            ip('link','set',name,'address',intent['system'],'mtu',str(network['mtu']))
            # Default-deny transit until the policy compiler supplies an
            # aggregate binding. Interface-local management still works.
            run('ip','netns','exec',NS,'nft','-f','-',input=vlan.guard(name));guarded=True
            from ffn_interface_management import apply
            apply(NS,name,dict(mode='l3',addresses=network['addresses'],management=network['management']))
            for address in network['addresses']:ip('address','add',address,'dev',name)
            units=vlan.reconcile(NS,name,token,network,ip,run)
            if vlan.carrying(network):ip('link','set',name,'up')
        except BaseException as error:
            problems=[]
            try:
                if created:teardown(name,token,network['management'])
                elif guarded:run('ip','netns','exec',NS,'nft','delete','table','inet','ffn_aggregate_'+name)
            except Exception as cleanup:problems.append(str(cleanup))
            if problems:raise RuntimeError('%s; cleanup errors: %s'%(error,problems)) from error
            raise
    return dict(ok=True,subinterfaces=units)


def network_teardown(request):
    """Remove what network-setup created; waits a bounded time for the lock."""
    name=request['group'];token=request['token'];management=request['management']
    if not re.fullmatch(r'ae(?:[1-9]|1[0-2])',str(name)):raise ValueError('Invalid aggregate name')
    import uuid
    if str(uuid.UUID(str(token)))!=token:raise ValueError('Invalid owner token')
    with open('/run/ffn-network.lock','a') as lock:
        deadline=time.monotonic()+20
        while True:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise RuntimeError('network lock busy; aggregate cleanup left for the watchdog')
                time.sleep(.05)
        teardown(name,token,management)
    return {'ok':True}


def apply_network_update(request):
    """Separate process: IP/nft/lease work must never block the LACP loop."""
    intent=validate(request['intent']);name=intent['group'];network=intent['network'];old=request['previous']
    path=Path('/run/ffn-aggregate-'+name+'-intent.json')
    with open('/run/ffn-network.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        current=json.loads(path.read_text())
        if current['token']!=intent['token'] or current.get('network_generation')!=network_revision(intent):raise ValueError('Network update superseded')
        link=json.loads(ip('-j','link','show','dev',name))[0]
        if link.get('ifalias')!='ffn-aggregate:'+intent['token']:raise ValueError('Aggregate network ownership changed')
        lease_path=Path('/run/ffn-aggregate-'+name+'-lease.json')
        lease=json.loads(lease_path.read_text()) if lease_path.exists() else {}
        if lease and lease.get('token')!=intent['token']:raise ValueError('Foreign DHCP lease')
        pending_path=Path('/run/ffn-aggregate-'+name+'-network-pending.json')
        pending=json.loads(pending_path.read_text()) if pending_path.exists() else {}
        if pending and pending.get('token')!=intent['token']:raise ValueError('Foreign network update journal')
        addresses=set(old['addresses'])|set(pending.get('addresses',[]))|({lease['address']} if lease.get('address') else set())
        # A failed worker can leave part of its address update installed. Keep
        # the exact owned set so the next update can remove that partial state.
        atomic(pending_path,dict(token=intent['token'],addresses=sorted(addresses|set(network['addresses']))))
        if lease.get('router'):
            routes=json.loads(ip('-j','route','show','default','dev',name))
            if any(route.get('gateway')==lease['router'] and str(route.get('protocol'))=='186' and route.get('metric')==old['dhcp_route_metric'] for route in routes):
                ip('route','del','default','via',lease['router'],'dev',name,'proto','186','metric',str(old['dhcp_route_metric']))
        ip('link','set',name,'down')
        actual={str(ipaddress.ip_interface(str(a['local'])+'/'+str(a['prefixlen']))) for n in json.loads(ip('-j','address','show','dev',name)) for a in n.get('addr_info',[])}
        for address in addresses&actual:ip('address','del',address,'dev',name)
        from ffn_interface_management import apply
        apply(NS,name,dict(mode='l3',addresses=network['addresses'],management=network['management']))
        ip('link','set',name,'mtu',str(network['mtu']))
        for address in network['addresses']:ip('address','add',address,'dev',name)
        lease_path.unlink(missing_ok=True)
        units=vlan.reconcile(NS,name,intent['token'],network,ip,run)
        if vlan.carrying(network):ip('link','set',name,'up')
        pending_path.unlink(missing_ok=True)
    return dict(revision=network_revision(intent),ok=True,subinterfaces=units)


def main(argv):
    verb=argv[1] if len(argv)>1 else ''
    if verb=='status':print(json.dumps(status()))
    elif verb=='validate':
        intent=validate(json.load(sys.stdin));print(json.dumps(dict(ok=True,revision=network_revision(intent))))
    elif verb=='network-setup':print(json.dumps(network_setup(json.load(sys.stdin))))
    elif verb=='network-update':print(json.dumps(apply_network_update(json.load(sys.stdin))))
    elif verb=='network-teardown':print(json.dumps(network_teardown(json.load(sys.stdin))))
    elif verb in ('serve','recover','sweep'):
        # The MP's pipe becomes the daemon's pipe unread: it reads the intent
        # (or the recovery identity) itself and keeps the contract from there.
        if not os.access(DAEMON,os.X_OK):
            print(json.dumps({'error':'ffn-portd is not installed on this dataplane'}),flush=True);raise SystemExit(1)
        os.execv(DAEMON,[DAEMON,'aggregate',verb])
    else:raise ValueError('Unknown aggregate owner operation')


if __name__=='__main__':
    try:main(sys.argv)
    except Exception as error:
        if len(sys.argv)>1 and sys.argv[1] in ('validate','network-setup','network-update','network-teardown'):
            print(json.dumps({'error':str(error)}),flush=True);raise SystemExit(1)
        raise
