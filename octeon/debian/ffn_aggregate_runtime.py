#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""MP-supervised OCTEON aggregate owner. A lost control pipe withdraws gates."""
import collections
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import time
sys.path.insert(0,'/usr/local/lib/ffn')
from ffn_aggregate_datapath import Gates
from ffn_lacp_engine import Engine
from ffn_lacp_trunk import TrunkLACP
from ffn_dp_packet_transport import FRONT,validate_trunk,decode_otmh_ssp,encode

NS='ffn-data'


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


def recover(name,token=None):
    if not re.fullmatch(r'ae(?:[1-9]|1[0-2])',name):raise ValueError('Invalid aggregate name')
    intent_path=Path('/run/ffn-aggregate-'+name+'-intent.json')
    if not intent_path.exists():return
    intent=json.loads(intent_path.read_text())
    if intent['boot_id']!=boot() or token is not None and intent['token']!=token:raise ValueError('Aggregate recovery identity changed')
    with open('/run/ffn-aggregate-'+name+'.lock','a') as owner:
        # An alive owner must be stopped through MP first. The watchdog may
        # terminate a stale process only after verifying PID start and command.
        try:fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            row=json.loads(Path('/run/ffn-aggregate-'+name+'-status.json').read_text())
            if time.monotonic()-row['updated_monotonic']<8:return
            proc=Path('/proc',str(row['pid']))
            start=(proc/'stat').read_text().rsplit(') ',1)[1].split()[19]
            args=(proc/'cmdline').read_bytes().split(b'\0')
            if row['token']!=intent['token'] or start!=row['process_start'] or not any(a.endswith(b'/ffn_aggregate_runtime.py') for a in args):raise ValueError('Stale owner process identity changed')
            os.kill(row['pid'],signal.SIGTERM);time.sleep(.5)
            try:fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                if (proc/'stat').read_text().rsplit(') ',1)[1].split()[19]!=start:raise ValueError('Owner PID changed')
                os.kill(row['pid'],signal.SIGKILL);time.sleep(.2)
                fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if not intent_path.exists():return
        if json.loads(intent_path.read_text())['token']!=intent['token']:raise ValueError('Recovery token changed')
        pidfile=Path('/run/ffn-aggregate-'+name+'-dhcp.pid')
        if pidfile.exists():
            pid=int(pidfile.read_text());proc=Path('/proc',str(pid))
            if proc.exists():
                args=(proc/'cmdline').read_bytes().split(b'\0')
                if b'udhcpc' not in [Path(a.decode()).name.encode() for a in args if a] or name.encode() not in args or b'/usr/local/sbin/ffn_aggregate_dhcp.py' not in args:
                    raise ValueError('DHCP process ownership changed')
                os.kill(pid,signal.SIGTERM)
                # The interface is still withdrawn if an unresponsive client
                # survives; subsequent hook calls reject the missing intent.
        with open('/run/ffn-network.lock','a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            links={p['ifname']:p for p in json.loads(ip('-j','link'))}
            if name in links:
                if links[name].get('ifalias')!='ffn-aggregate:'+intent['token']:raise ValueError('Refusing to delete an unowned aggregate interface')
                ip('link','set',name,'down')
                from ffn_interface_management import apply
                apply(NS,name,dict(mode='l3',addresses=[],management=intent['network']['management']),remove=True)
                ip('link','delete',name)
            tables=json.loads(run('ip','netns','exec',NS,'nft','-j','list','tables'))['nftables']
            if any(t.get('table',{}).get('name')=='ffn_aggregate_'+name for t in tables):run('ip','netns','exec',NS,'nft','delete','table','inet','ffn_aggregate_'+name)
        for suffix in ('status.json','lease.json','dhcp.pid','intent.json'):
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
    if set(network)!={'addresses','dhcp','dhcp_default_route','dhcp_route_metric','mtu','management'}:raise ValueError('Invalid aggregate network settings')
    if type(network['mtu']) is not int or not 576<=network['mtu']<=1500:raise ValueError('Aggregate packet path supports MTU 576..1500')
    if any(type(network[k]) is not bool for k in ('dhcp','dhcp_default_route')):raise ValueError('Invalid DHCP settings')
    if type(network['dhcp_route_metric']) is not int or not 1<=network['dhcp_route_metric']<=65535:raise ValueError('Invalid route metric')
    if not isinstance(network['addresses'],list) or len(network['addresses'])>32:raise ValueError('Invalid addresses')
    for address in network['addresses']:ipaddress.ip_interface(address)
    if network['dhcp'] and network['addresses']:raise ValueError('Choose DHCP or static addressing')
    from ffn_interface_management import validate as validate_management
    validate_management(network['management'])
    lacp=intent['lacp']
    if set(lacp)!={'activity','rate','min_links','system_priority'}:raise ValueError('Invalid LACP settings')
    # Engine validates all protocol parameters and the stable system MAC.
    Engine(intent['system'],int(intent['group'][2:]),{p:intent['system'] for p in members},Gates(members),**lacp)
    return intent


def tap(name):
    original=os.open('/proc/self/ns/net',os.O_RDONLY);target=os.open('/run/netns/'+NS,os.O_RDONLY);fd=None
    try:
        os.setns(target,0);fd=os.open('/dev/net/tun',os.O_RDWR|os.O_NONBLOCK)
        request=0x800454ca if platform.machine().startswith('mips') else 0x400454ca
        fcntl.ioctl(fd,request,struct.pack('16sH',name.encode(),0x1002))
        return fd
    except BaseException:
        if fd is not None:os.close(fd)
        raise
    finally:os.setns(original,0);os.close(original);os.close(target)


def lldp(system,group,port):
    mac=bytes.fromhex(system.replace(':',''))
    def tlv(kind,value):return struct.pack('!H',(kind<<9)|len(value))+value
    return (bytes.fromhex('0180c200000e')+mac+b'\x88\xcc'+tlv(1,b'\x04'+mac)+
        tlv(2,b'\x05'+('ethernet1/'+str(port)).encode())+tlv(3,struct.pack('!H',120))+
        tlv(4,('FFN aggregate '+group).encode())+tlv(5,b'FFN-NGFW')+b'\0\0')


def serve(intent):
    intent=validate(intent);name=intent['group'];members=intent['members'];network=intent['network']
    if 'octeon' not in Path('/proc/cpuinfo').read_text().lower():raise ValueError('OCTEON dataplane required')
    validate_trunk('ffnpkt0')
    run('systemctl','is-active','--quiet','ffn-aggregate-dp-watchdog.timer')
    state_path=Path('/run/ffn-aggregate-'+name+'-status.json')
    intent_path=Path('/run/ffn-aggregate-'+name+'-intent.json')
    gates=Gates(members);engine=Engine(intent['system'],int(name[2:]),{p:intent['system'] for p in members},gates,
        collecting=not intent['control_only'],**intent['lacp'])
    from ffn_aggregate_offload import Offload
    offload=Offload(int(name[2:]),members) if intent['offload'] else None
    fd=None;wire=None;inspector=None;dhcp=None;created=False;guarded=False;counts=collections.Counter()
    def halt(*_):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,halt)
    with contextlib.ExitStack() as stack:
        # Legacy fabric owners take EX; scoped WAN/aggregate owners take SH and
        # individual port EX locks, allowing coexistence without overlapping RX.
        fabric=stack.enter_context(open('/run/ffn-fabric.lock','a'));fcntl.flock(fabric,fcntl.LOCK_SH|fcntl.LOCK_NB)
        for key in [name]+['port-'+str(p) for p in sorted(members)]:
            lock=stack.enter_context(open('/run/ffn-aggregate-'+key+'.lock','a'));fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            # Persist ownership before any netdevice mutation so the watchdog
            # can recover a crash during initialization as well as normal work.
            atomic(intent_path,intent)
            if not intent['control_only']:
                with open('/run/ffn-network.lock','a') as lock:
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    if any(n['ifname']==name for n in json.loads(ip('-j','link'))):raise ValueError('Aggregate netdevice already exists; refusing adoption')
                    ip('tuntap','add','dev',name,'mode','tap');created=True
                    ip('link','set',name,'alias','ffn-aggregate:'+intent['token'])
                    ip('link','set',name,'address',intent['system'],'mtu',str(network['mtu']))
                    # Default-deny transit until the policy compiler supplies an
                    # aggregate binding. Interface-local management still works.
                    script='table inet ffn_aggregate_'+name+' {\n chain forward {\n type filter hook forward priority -250; policy accept;\n iifname "'+name+'" counter drop;\n oifname "'+name+'" counter drop;\n }\n}\n'
                    run('ip','netns','exec',NS,'nft','-f','-',input=script);guarded=True
                    from ffn_interface_management import apply
                    settings=dict(mode='l3',addresses=network['addresses'],management=network['management'])
                    apply(NS,name,settings)
                    for address in network['addresses']:ip('address','add',address,'dev',name)
                    fd=tap(name);ip('link','set',name,'up')
                from ffn_inspection import Inspector
                inspector=Inspector(status_path=Path('/run/ffn-inspection-'+name+'.json'))
            wire=socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3));wire.bind(('ffnpkt0',0));wire.setblocking(False)
            adapter=TrunkLACP(engine,wire)
            started=time.monotonic();next_status=0;next_lldp=0;last_input=started;sequence=-1;buffer=b''
            local={ipaddress.ip_interface(a).ip.packed for a in network['addresses']}
            while True:
                now=time.monotonic()
                if now-last_input>(60 if sequence<0 else 8):raise RuntimeError('MP/CP observation lease expired')
                engine.tick(now)
                adapter.service(now)
                if engine.fault:raise RuntimeError(engine.fault)
                if inspector:inspector.tick()
                if now>=next_lldp and intent['lldp']:
                    for p,m in engine.members.items():
                        if m['link']:
                            packet=encode(p,lldp(intent['system'],name,p))
                            if wire.send(packet)!=len(packet):raise OSError('Short LLDP write')
                    next_lldp=now+30
                result=engine.status(now)
                if not intent['control_only'] and network['dhcp'] and result['distributing'] and dhcp is None:
                    dhcp=subprocess.Popen(['ip','netns','exec',NS,'udhcpc','-f','-i',name,'-s','/usr/local/sbin/ffn_aggregate_dhcp.py','-p','/run/ffn-aggregate-'+name+'-dhcp.pid'],stdout=sys.stderr,stderr=sys.stderr,start_new_session=True)
                if dhcp is not None and dhcp.poll() is not None:raise RuntimeError('Aggregate DHCP client exited')
                if now>=next_status:
                    next_status=now+1
                    lease=Path('/run/ffn-aggregate-'+name+'-lease.json')
                    lease_data=json.loads(lease.read_text()) if lease.exists() else {}
                    if network['dhcp']:
                        local={ipaddress.ip_interface(lease_data['address']).ip.packed} if lease_data.get('token')==intent['token'] and lease_data.get('address') else set()
                    row=dict(result,group=name,token=intent['token'],boot_id=boot(),pid=os.getpid(),
                        process_start=Path('/proc/self/stat').read_text().rsplit(') ',1)[1].split()[19],updated_monotonic=now,
                        control_only=intent['control_only'],attachment_ready=bool(result['distributing']) and fd is not None,
                        network=network,ports=members,hardware_offload=bool(offload and offload.ready(gates,now)),
                        offload_requested=intent['offload'],offload_tx=offload.transmitted if offload else 0,
                        offload_scope='BCM egress member selection only',transit_policy='default-deny',counters=dict(counts),
                        data_rx=dict(gates.rx),data_tx=dict(gates.tx),gate_drops=gates.dropped,
                        network_ready=not network['dhcp'] or bool(local) and not lease_data.get('error'),lease=lease_data)
                    atomic(state_path,row);print(json.dumps(row),flush=True)
                ready,_,_=select.select([sys.stdin.fileno(),wire]+([fd] if fd is not None else []),[],[],.05)
                # Drain control before packets so a withdrawal closes gates first.
                if sys.stdin.fileno() in ready:
                    data=os.read(sys.stdin.fileno(),65536)
                    if not data:raise RuntimeError('MP control pipe closed')
                    buffer+=data
                    if len(buffer)>262144:raise ValueError('Control input exceeds limit')
                    while b'\n' in buffer:
                        line,buffer=buffer.split(b'\n',1);msg=json.loads(line)
                        if msg.get('token')!=intent['token'] or type(msg.get('sequence')) is not int or msg['sequence']<=sequence:raise ValueError('Stale aggregate observation')
                        sequence=msg['sequence'];age=msg.get('age_seconds')
                        if type(age) not in (int,float) or not 0<=age<5:raise ValueError('Expired CP observation')
                        links=msg.get('links',[])
                        if len(links)!=len(members) or {p['port'] for p in links}!=set(members):raise ValueError('Incomplete member observation')
                        stamp=time.monotonic()
                        for p in links:engine.link(p['port'],p['up'],p['speed_mbps'] if p['up'] else 0,stamp,lease_seconds=6-age)
                        if offload:offload.acknowledge(msg.get('offload'),stamp,age)
                        last_input=stamp
                if wire in ready:
                    for _ in range(128):
                        try:raw,address=wire.recvfrom(16384)
                        except BlockingIOError:break
                        if address[2]==socket.PACKET_OUTGOING:continue
                        if offload:raw=offload.ingress(raw)
                        if adapter.receive(raw,time.monotonic()):continue
                        item=decode_otmh_ssp(raw,set(members))
                        if item is None:continue
                        port,frame=item
                        if len(frame)>network['mtu']+18:counts['oversize_drop']+=1;continue
                        if fd is None:counts['control_only_drop']+=1;continue
                        if frame[12:14]==b'\x88\xcc':continue
                        destination=frame[30:34] if frame[12:14]==b'\x08\x00' and len(frame)>=34 else frame[38:54] if frame[12:14]==b'\x86\xdd' and len(frame)>=54 else None
                        if destination not in local and not inspector.allow(port,frame):counts['inspection_drop']+=1;continue
                        def deliver(_port,payload):
                            if os.write(fd,payload)!=len(payload):raise OSError('Short aggregate TAP write')
                        engine.tick(time.monotonic())
                        try:gates.receive(port,frame,deliver)
                        except BlockingIOError:counts['rx_queue_drop']+=1
                if fd is not None and fd in ready:
                    frame=os.read(fd,network['mtu']+19)
                    if not 14<=len(frame)<=network['mtu']+18:counts['tx_length_drop']+=1;continue
                    def send(port,payload):
                        packet=encode(port,payload)
                        if wire.send(packet)!=len(packet):raise OSError('Short aggregate data write')
                    engine.tick(time.monotonic())
                    def send_hardware(packet):
                        if wire.send(packet)!=len(packet):raise OSError('Short hardware aggregate write')
                    try:
                        if not offload or not offload.transmit(frame,gates,time.monotonic(),send_hardware):
                            gates.transmit(frame,send)
                    except BlockingIOError:counts['tx_queue_drop']+=1
        finally:
            engine.stop(time.monotonic())
            if dhcp is not None:
                if dhcp.poll() is None:
                    os.killpg(dhcp.pid,signal.SIGTERM)
                    try:dhcp.wait(timeout=3)
                    except subprocess.TimeoutExpired:os.killpg(dhcp.pid,signal.SIGKILL);dhcp.wait()
            if inspector:inspector.close()
            if wire:wire.close()
            if fd is not None:os.close(fd)
            if created:
                with open('/run/ffn-network.lock','a') as lock:
                    fcntl.flock(lock,fcntl.LOCK_EX)
                    ip('link','set',name,'down')
                    from ffn_interface_management import apply
                    apply(NS,name,dict(mode='l3',addresses=[],management=network['management']),remove=True)
                    ip('link','delete',name)
            if guarded:run('ip','netns','exec',NS,'nft','delete','table','inet','ffn_aggregate_'+name)
            for suffix in ('lease.json','dhcp.pid'):
                Path('/run/ffn-aggregate-'+name+'-'+suffix).unlink(missing_ok=True)
            state_path.unlink(missing_ok=True);intent_path.unlink(missing_ok=True)


if __name__=='__main__':
    if sys.argv[1]=='status':print(json.dumps(status()))
    elif sys.argv[1]=='recover':
        request=json.load(sys.stdin)
        if set(request)!={'group','token','boot_id'} or request['boot_id']!=boot():raise ValueError('Fresh DP recovery identity required')
        recover(request['group'],request['token']);print(json.dumps({'recovered':True}))
    elif sys.argv[1]=='sweep':
        for path in Path('/run').glob('ffn-aggregate-ae*-intent.json'):
            intent=json.loads(path.read_text())
            try:recover(intent['group'],intent['token'])
            except (FileNotFoundError,BlockingIOError):pass
    elif sys.argv[1]=='serve':
        # Read exactly the initial line; do not let a buffered wrapper consume
        # subsequent control frames ahead of the select loop's os.read().
        raw=b''
        while not raw.endswith(b'\n'):
            byte=os.read(0,1)
            if not byte or len(raw)>65536:raise ValueError('Invalid initial intent')
            raw+=byte
        serve(json.loads(raw))
    else:raise ValueError('Unknown aggregate owner operation')
