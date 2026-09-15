#!/usr/bin/env python3
"""Single-owner VIF netdevices, revisioned control and direct DP forwarding.

No BCM/FE100 provisioning. The selected platform commissions the trunk and
drains hardware sessions before assignment changes. Unassigned packets drop.
"""
import argparse
import collections
import fcntl
import json
import os
from pathlib import Path
import platform
import select
import signal
import socket
import struct
import stat
import subprocess
import sys
import time
import uuid
from ffn_vif import Assignments,validate
import ffn_network as network

STATE=Path('/etc/ffn/vifs.json')
INTENT=Path('/etc/ffn/vifs.pending.json')
SOCKET='/run/ffn-vif.sock'


def atomic(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp')
    with tmp.open('w') as f:
        json.dump(value,f);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)
    fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


class Linux:
    def links(self):return {p['ifname']:p for p in json.loads(network.ip('-d','-j','link'))}

    def verify(self,config):
        import ipaddress
        links=self.links()
        addresses={p['ifname']:p.get('addr_info',[]) for p in json.loads(network.ip('-j','address'))}
        for name,b in config['vifs'].items():
            link=links.get(name,{})
            s=b['network'] if b['enabled'] else {'mode':'disabled','mtu':b['network'].get('mtu',1500)}
            master='br-data' if s['mode']=='l2' else s.get('vrf')
            wanted={str(ipaddress.ip_interface(a)) for a in s.get('addresses',[])}
            actual={str(ipaddress.ip_interface(a['local']+'/'+str(a['prefixlen'])))
                    for a in addresses.get(name,[]) if a.get('scope')!='link' or
                    str(ipaddress.ip_interface(a['local']+'/'+str(a['prefixlen']))) in wanted}
            if (link.get('ifalias')!='ffn:vif:'+name or link.get('linkinfo',{}).get('info_kind')!='tun'
                    or link.get('master')!=master or link.get('mtu')!=s.get('mtu',1500)
                    or ('UP' in link.get('flags',[]))!=(s['mode']!='disabled') or actual!=wanted):
                raise RuntimeError('VIF netdevice configuration drift: '+name)
            if s['mode']=='l2':
                rows=json.loads(network.run('ip','netns','exec',network.NS,'bridge','-j','vlan','show','dev',name))
                vlans=[v for row in rows for v in row.get('vlans',[])]
                if len(vlans)!=1 or vlans[0]['vlan']!=s['pvid'] or set(vlans[0].get('flags',[]))!={'PVID','Egress Untagged'}:
                    raise RuntimeError('VIF bridge membership drift: '+name)

    def preflight(self,old,new):
        if not network.exists():raise RuntimeError('ffn-data networking must be started')
        links=self.links()
        for name in set(old['vifs'])|set(new['vifs']):
            link=links.get(name)
            if link and (link.get('ifalias')!='ffn:vif:'+name or link.get('linkinfo',{}).get('info_kind')!='tun'):
                raise RuntimeError('VIF name is owned by another interface: '+name)
            if link and name not in old['vifs']:
                raise RuntimeError('existing VIF is not in the owner journal: '+name)
        for b in new['vifs'].values():
            vrf=b['network'].get('vrf')
            if vrf and links.get(vrf,{}).get('linkinfo',{}).get('info_kind')!='vrf':
                raise ValueError('configure the referenced VRF first')
        # Do not silently remove routes installed by another controller.
        changed={n for n in old['vifs'] if old['vifs'].get(n)!=new['vifs'].get(n)}
        for family in ('-4','-6'):
            for route in json.loads(network.ip(family,'-j','route','show','table','all')):
                devs={route.get('dev')}|{h.get('dev') for h in route.get('nexthops',[])}
                if changed&devs and route.get('protocol')!='kernel':
                    raise RuntimeError('remove dependent static/dynamic VIF routes before reassignment')
        occupied=set()
        for interface in json.loads(network.ip('-j','address')):
            name=interface['ifname']
            if name in old['vifs']:continue
            master=links.get(name,{}).get('master')
            vrf=master if links.get(master,{}).get('linkinfo',{}).get('info_kind')=='vrf' else None
            for address in interface.get('addr_info',[]):occupied.add((vrf,address['local']))
        import ipaddress
        for b in new['vifs'].values():
            for address in b['network'].get('addresses',[]):
                if (b['network'].get('vrf'),str(ipaddress.ip_interface(address).ip)) in occupied:
                    raise ValueError('VIF address already belongs to another interface')

    def reconcile(self,old,new):
        links=self.links()
        for name in set(old['vifs'])|set(new['vifs']):
            before,after=old['vifs'].get(name),new['vifs'].get(name)
            if before==after and name in links:continue
            if name in links:
                if links[name].get('ifalias')!='ffn:vif:'+name:raise RuntimeError('lost VIF ownership')
                network.ip('link','delete',name)
            if after:
                network.run('ip','netns','exec',network.NS,'ip','tuntap','add','dev',name,'mode','tap')
                network.ip('link','set',name,'alias','ffn:vif:'+name)
                settings=after['network'] if after['enabled'] else {'mode':'disabled','mtu':after['network'].get('mtu',1500)}
                network.configure_port(name,settings)
        self.verify(new)

    def open(self,name):
        original=os.open('/proc/self/ns/net',os.O_RDONLY);target=os.open('/run/netns/'+network.NS,os.O_RDONLY)
        fd=None
        try:
            os.setns(target,0);fd=os.open('/dev/net/tun',os.O_RDWR|os.O_NONBLOCK)
            ioctl=0x800454ca if platform.machine().startswith('mips') else 0x400454ca
            fcntl.ioctl(fd,ioctl,struct.pack('16sH',name.encode(),0x1002))
            # Discard queued frames before publishing any new port binding.
            for _ in range(8192):
                try:os.read(fd,65536)
                except BlockingIOError:break
            else:raise RuntimeError('VIF queue did not drain')
            return fd
        except BaseException:
            if fd is not None:os.close(fd)
            raise
        finally:os.setns(original,0);os.close(original);os.close(target)


class Owner:
    def __init__(self,backend,ports,state=STATE,intent=INTENT):
        self.backend,self.ports,self.path,self.intent=backend,set(ports),state,intent
        self.config=validate(json.loads(state.read_text())) if state.exists() else {'revision':0,'vifs':{}}
        self.recovery_required=intent.exists()
        self.assignment=Assignments(self.config,self.ports)

    def status(self):
        return {'config':self.config,'recovery_required':self.recovery_required,
                'ports':sorted(self.ports),'physical_link':None,'hardware_session_offload':False}

    def replace(self,request):
        if self.recovery_required:raise RuntimeError('VIF recovery required')
        wanted=validate(request)
        if wanted['revision']!=self.config['revision']:raise ValueError('revision conflict')
        wanted['revision']+=1;mapping=Assignments(wanted,self.ports)
        self.backend.preflight(self.config,wanted)
        old=self.config
        atomic(self.intent,{'before':old,'wanted':wanted})
        self.recovery_required=True
        try:
            self.backend.reconcile(old,wanted)
            atomic(self.path,wanted)
        except BaseException:
            # Recreate even partially configured devices from the saved state.
            try:
                self.backend.reconcile(wanted,old)
                atomic(self.path,old)
                self.intent.unlink();self.recovery_required=False
            except BaseException:pass
            raise
        self.config=wanted;self.assignment=mapping
        self.intent.unlink();self.recovery_required=False
        return self.status()

    def recover(self):
        if not self.recovery_required:return self.status()
        record=json.loads(self.intent.read_text())
        before,wanted=validate(record['before']),validate(record['wanted'])
        self.backend.preflight(wanted,before)
        self.backend.reconcile(wanted,before);atomic(self.path,before)
        self.config=before;self.assignment=Assignments(before,self.ports)
        self.intent.unlink();self.recovery_required=False
        return self.status()


def read_request(conn):
    conn.settimeout(3);data=bytearray()
    while not data.endswith(b'\n'):
        part=conn.recv(65536-len(data))
        if not part or len(data)+len(part)>=65536:raise ValueError('invalid bounded VIF request')
        data.extend(part)
    request=json.loads(data)
    if not isinstance(request,dict) or set(request)!={'op','payload'}:raise ValueError('invalid request')
    return request


def rpc(op,payload):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as conn:
        conn.settimeout(60);conn.connect(SOCKET)
        conn.sendall(json.dumps({'op':op,'payload':payload}).encode()+b'\n')
        data=bytearray()
        while not data.endswith(b'\n'):
            part=conn.recv(65536)
            if not part or len(data)+len(part)>1024*1024:raise RuntimeError('VIF outcome unknown; refresh status')
            data.extend(part)
    result=json.loads(data)
    if not result.get('ok'):raise RuntimeError(result.get('error','VIF operation failed'))
    return result['result']


def serve(owner,trunk,seconds):
    from ffn_dp_packet_transport import validate_trunk,encode,decode_otmh_ssp
    from ffn_inspection import Inspector
    validate_trunk(trunk)
    handles={};counts=collections.Counter();generation=str(uuid.uuid4());runtime_error=None;running=True
    def close():
        for fd in handles.values():os.close(fd)
        handles.clear()
    def attach():
        close()
        if owner.recovery_required:return
        try:
            owner.backend.verify(owner.config)
            for name in owner.config['vifs']:handles[name]=owner.backend.open(name)
        except BaseException:close();raise
    def status():
        return owner.status()|{'running':running,'runtime_error':runtime_error,'generation':generation,'trunk':trunk,
            'forwarding':bool(handles) and not owner.recovery_required and len(handles)==len(owner.config['vifs'])
                and any(b['enabled'] and b['network']['mode']!='disabled' for b in owner.config['vifs'].values()),
            'counters':dict(counts)}
    # The lifetime fabric lock prevents a second TAP/physical packet consumer.
    with open('/run/ffn-fabric.lock','a') as fabric,open('/run/ffn-network.lock','a') as netlock:
        fcntl.flock(fabric,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if owner.recovery_required:raise RuntimeError('recover interrupted VIF changes first')
        fcntl.flock(netlock,fcntl.LOCK_EX)
        try:owner.backend.preflight(owner.config,owner.config);owner.backend.reconcile(owner.config,owner.config);attach()
        finally:fcntl.flock(netlock,fcntl.LOCK_UN)
        inspector=Inspector()
        with socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3)) as wire,socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as server:
            wire.bind((trunk,0));wire.setblocking(False)
            if Path(SOCKET).exists():
                if not stat.S_ISSOCK(Path(SOCKET).lstat().st_mode):raise RuntimeError('control socket path conflict')
                Path(SOCKET).unlink()
            server.bind(SOCKET);os.chmod(SOCKET,0o600);server.listen(4)
            deadline=time.monotonic()+seconds if seconds else float('inf')
            next_verify=time.monotonic()+2
            try:
                while time.monotonic()<deadline:
                    if handles and time.monotonic()>=next_verify:
                        try:owner.backend.verify(owner.config)
                        except Exception as e:close();runtime_error=str(e)
                        next_verify=time.monotonic()+2
                    inspector.tick();byfd={fd:name for name,fd in handles.items()}
                    ready,_,_=select.select([wire,server,*byfd],[],[],.25)
                    if server in ready:
                        conn,_=server.accept()
                        with conn:
                            op=None
                            try:
                                request=read_request(conn);op=request['op'];payload=request['payload']
                                if op=='status' and not payload:result=status()
                                elif op=='check':
                                    Assignments(payload,owner.ports)
                                    if payload['revision']!=owner.config['revision']:raise ValueError('revision conflict')
                                    owner.backend.preflight(owner.config,payload);result={'validated':True}
                                elif op=='set':
                                    fcntl.flock(netlock,fcntl.LOCK_EX);close()
                                    try:owner.replace(payload)
                                    finally:
                                        try:
                                            # Never classify an old RX backlog with new bindings.
                                            for _ in range(8192):
                                                try:wire.recv(65536);counts['reconfigure_rx_drop']+=1
                                                except BlockingIOError:break
                                            else:raise RuntimeError('trunk queue did not drain')
                                            attach()
                                            runtime_error=None
                                        finally:fcntl.flock(netlock,fcntl.LOCK_UN)
                                    result=status()
                                else:raise ValueError('unsupported live VIF operation')
                                reply={'ok':True,'result':result}
                            except Exception as e:
                                if op=='set':runtime_error=str(e)
                                reply={'ok':False,'error':str(e)}
                            try:conn.sendall(json.dumps(reply).encode()+b'\n')
                            except OSError:pass
                        continue # old descriptors and bindings are never reused
                    for source in ready:
                        try:
                            if source is wire:
                                raw,addr=wire.recvfrom(65536)
                                if addr[2]==socket.PACKET_OUTGOING:continue
                                decoded=decode_otmh_ssp(raw,owner.ports)
                                item=owner.assignment.ingress(*decoded) if decoded and not owner.recovery_required else None
                                if not item:counts['unassigned_or_invalid_rx']+=1;continue
                                name,frame=item
                                if not inspector.allow(decoded[0],decoded[1]):counts['inspection_drop']+=1;continue
                                if name not in handles or os.write(handles[name],frame)!=len(frame):raise OSError('short VIF RX')
                                counts['rx_'+name]+=1
                            else:
                                name=byfd[source];frame=os.read(source,1519)
                                item=owner.assignment.egress(name,frame) if not owner.recovery_required else None
                                if not item:counts['unassigned_or_invalid_tx']+=1;continue
                                packet=encode(*item)
                                if wire.send(packet)!=len(packet):raise OSError('short trunk TX')
                                counts['tx_'+name]+=1
                        except (OSError,ValueError):counts['io_drop']+=1
            finally:
                running=False
                close();inspector.close();Path(SOCKET).unlink(missing_ok=True)
                print(json.dumps(status()),flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('status','check','set','recover','serve','start','stop'))
    p.add_argument('--ports',default='5,13');p.add_argument('--trunk',default='ffnpkt0')
    p.add_argument('--seconds',type=int,default=0);a=p.parse_args()
    ports={int(x) for x in a.ports.split(',')}
    if not ports or ports-set(range(1,25)) or not 0<=a.seconds<=3600:p.error('invalid transport limits')
    if a.action in ('start','stop'):
        subprocess.run(['systemctl',a.action,'ffn-vif.service'],check=True,timeout=30)
        if a.action=='start':
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                try:print(json.dumps(rpc('status',{})));return
                except OSError:time.sleep(.2)
            raise RuntimeError('VIF service did not become ready')
        a.action='status'
    payload={}
    if a.action in ('set','check'):
        raw=sys.stdin.buffer.read(65537)
        if len(raw)>65536:raise ValueError('request too large')
        payload=json.loads(raw)
    if a.action not in ('serve','recover') and Path(SOCKET).exists():
        try:print(json.dumps(rpc(a.action,payload)));return
        except (ConnectionRefusedError,FileNotFoundError):pass
    with open('/run/ffn-vif.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if Path(SOCKET).exists():
            if not stat.S_ISSOCK(Path(SOCKET).lstat().st_mode):raise RuntimeError('control socket path conflict')
            Path(SOCKET).unlink()
        owner=Owner(Linux(),ports)
        if a.action=='serve':
            cpu=Path('/proc/cpuinfo').read_text()
            if 'octeon' not in cpu.lower() or sum(l.startswith('processor') for l in cpu.splitlines())<16:
                raise RuntimeError('OCTEON dataplane required')
            signal.signal(signal.SIGTERM,lambda *_:(_ for _ in ()).throw(KeyboardInterrupt()))
            try:serve(owner,a.trunk,a.seconds)
            except KeyboardInterrupt:pass
            return
        if a.action in ('set','recover'):
            with open('/run/ffn-network.lock','a') as netlock:
                fcntl.flock(netlock,fcntl.LOCK_EX)
                result=owner.replace(payload) if a.action=='set' else owner.recover()
        elif a.action=='check':
            Assignments(payload,ports)
            if payload['revision']!=owner.config['revision']:raise ValueError('revision conflict')
            owner.backend.preflight(owner.config,payload);result={'validated':True}
        else:result=owner.status()
        print(json.dumps(result|{'running':False,'forwarding':False}))


if __name__=='__main__':main()
