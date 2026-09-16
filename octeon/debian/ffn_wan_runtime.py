#!/usr/bin/env python3
"""MP-selected WAN1 direct OCTEON packet attachment, no management-plane relay.

CP owns the BCM redirect and qualification. This root-only DP owner attaches
only p1, shares the fabric ownership lock, and never changes BCM/PHY settings.
"""
import collections
import fcntl
import json
import ipaddress
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from ffn_dp_packet_transport import validate_trunk,decode_otmh_ssp
import ffn_dp_packet_transport as transport
from ffn_fabric import tap

INTENT=Path('/run/ffn-wan-attachment.json')
STATE=Path('/run/ffn-fabric.json')
FRONT={1:28}


def boot():return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def status():
    value=json.loads(STATE.read_text()) if STATE.exists() else {}
    running=False
    if value.get('owner')=='wan1' and value.get('boot_id')==boot():
        try:
            start=Path('/proc',str(value['pid']),'stat').read_text().rsplit(') ',1)[1].split()[19]
            running=start==value.get('process_start')
        except (OSError,KeyError):pass
    return {'running':running,'boot_id':boot(),'attachment':value if running else {},'hardware_offload':False}


def serve():
    intent=json.loads(INTENT.read_text())
    if intent.get('boot_id')!=boot() or intent.get('front')!={'1':28}:
        raise ValueError('Fresh MP-selected WAN attachment required')
    if 'octeon' not in Path('/proc/cpuinfo').read_text().lower():raise ValueError('OCTEON required')
    validate_trunk('ffnpkt0')
    handles={};sockets=[];counts=collections.Counter();inspector=None
    def halt(*_):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,halt)
    with open('/run/ffn-fabric.lock','a') as owner:
        fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            with open('/run/ffn-network.lock','a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                handles[1]=tap(1)
                value={'owner':'wan1','pid':os.getpid(),'boot_id':boot(),
                       'process_start':Path('/proc/self/stat').read_text().rsplit(') ',1)[1].split()[19],
                       'ports':[1],'max_mtu':1500,'transport':'direct OCTEON WAN','hardware_offload':False}
                STATE.write_text(json.dumps(value))
            for _ in range(2):
                sock=socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3));sock.bind(('ffnpkt0',0));sockets.append(sock)
            # Only WAN1 is attached. Local service permissions are enforced by
            # the kernel INPUT hook; transit Security belongs in FORWARD.
            from ffn_inspection import Inspector
            inspector=Inspector()
            class Observation:
                next_poll=0
                local=set()
                def allow(self,port,frame):
                    destination=None
                    if len(frame)>=34 and frame[12:14]==b'\x08\x00' and frame[14]>>4==4:
                        destination=frame[30:34]
                    elif len(frame)>=54 and frame[12:14]==b'\x86\xdd' and frame[14]>>4==6:
                        destination=frame[38:54]
                    if destination in self.local:
                        counts['local_input']+=1
                        return True  # The interface profile INPUT hook owns this decision.
                    return inspector.allow(port,frame)
                def tick(self):
                    if time.monotonic()<self.next_poll:return
                    self.next_poll=time.monotonic()+1
                    inspector.tick()
                    cfg=json.loads(Path('/etc/ffn/network.json').read_text())
                    self.local={ipaddress.ip_interface(a).ip.packed for a in cfg['ports'].get('p1',{}).get('addresses',[])}
                    value['counters']=dict(counts);value['updated_at']=time.time()
                    temp=STATE.with_suffix('.tmp');temp.write_text(json.dumps(value));temp.replace(STATE)
            transport.FRONT=FRONT
            original_encode=transport.encode
            transport.encode=lambda port,frame:original_encode(port,frame,FRONT)
            transport.pump(*sockets,handles,Observation(),counters=counts,
                           decoder=lambda frame,ports:decode_otmh_ssp(frame,ports,FRONT))
        except KeyboardInterrupt:pass
        finally:
            if inspector:inspector.close()
            for sock in sockets:sock.close()
            for fd in handles.values():os.close(fd)
            STATE.unlink(missing_ok=True)


def execute(action,payload):
    if action=='status' and not payload:return status()
    if action not in ('start','stop') or set(payload)!={'boot_id'} or payload['boot_id']!=boot():
        raise ValueError('Current DP boot identity required')
    if action=='start':
        if status()['running']:return status()
        with open('/run/ffn-fabric.lock','a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            INTENT.write_text(json.dumps({'boot_id':boot(),'front':{'1':28}}));INTENT.chmod(0o600)
    subprocess.run(['systemctl',action,'ffn-wan-attachment.service'],check=True,timeout=20)
    if action=='start':
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            if status()['running']:return status()
            time.sleep(.1)
        raise RuntimeError('WAN packet attachment did not become ready')
    return status()


if __name__=='__main__':
    if sys.argv[1]=='serve':serve()
    else:print(json.dumps(execute(sys.argv[1],json.load(sys.stdin))))
