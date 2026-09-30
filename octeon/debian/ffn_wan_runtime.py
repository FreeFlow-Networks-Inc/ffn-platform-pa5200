#!/usr/bin/env python3
"""MP-selected WAN1 direct OCTEON packet attachment, no management-plane relay.

CP owns the BCM redirect and qualification. This root-only DP owner attaches
only p1, shares the fabric ownership lock, and never changes BCM/PHY settings.
"""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from ffn_dp_packet_transport import validate_trunk
import ffn_dp_packet_transport as transport
from ffn_native_packet import PacketOwner

INTENT=Path('/run/ffn-wan-attachment.json')
STATE=Path('/run/ffn-fabric.json')
FRONT={1:28}
PORT=1
OWNER='wan1'
UNIT='ffn-wan-attachment.service'


def select_physical(port):
    """Select one commissioned board port in an independent owner process."""
    global PORT,OWNER,UNIT,FRONT,INTENT,STATE
    mapping=dict(transport.FRONT);mapping.update({1:28,2:13,3:14,4:15})
    if type(port) is not int or port not in mapping or port==1:
        raise ValueError('Invalid independent physical port; port 1 retains its WAN owner')
    PORT=port;OWNER='physical-'+str(port);FRONT={port:mapping[port]}
    UNIT='ffn-physical@'+str(port)+'.service'
    INTENT=Path('/run/ffn-physical-'+str(port)+'-intent.json')
    STATE=Path('/run/ffn-physical-'+str(port)+'-status.json')


def boot():return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def status():
    value=json.loads(STATE.read_text()) if STATE.exists() else {}
    running=False
    if value.get('owner')==OWNER and value.get('boot_id')==boot():
        try:
            process=Path('/proc',str(value['pid']),'stat').read_text().rsplit(') ',1)[1].split()
            running=(process[19]==value.get('process_start') and process[0]!='Z'
                     and 0<=time.monotonic()-value['updated_monotonic']<=5)
        except (OSError,KeyError,IndexError,TypeError):pass
    return {'running':running,'boot_id':boot(),'attachment':value if running else {},'hardware_offload':False}


def serve():
    intent=json.loads(INTENT.read_text())
    if intent.get('boot_id')!=boot() or intent.get('front')!={str(k):v for k,v in FRONT.items()}:
        raise ValueError('Fresh MP-selected WAN attachment required')
    if 'octeon' not in Path('/proc/cpuinfo').read_text().lower():raise ValueError('OCTEON required')
    validate_trunk('ffnpkt0')
    native=None;inspector=None
    def halt(*_):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,halt)
    with open('/run/ffn-fabric.lock','a') as owner, open('/run/ffn-aggregate-port-'+str(PORT)+'.lock','a') as member_owner:
        fcntl.flock(owner,fcntl.LOCK_SH|fcntl.LOCK_NB)
        fcntl.flock(member_owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            with open('/run/ffn-network.lock','a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                native=PacketOwner(PORT,FRONT[PORT])
                value={'owner':OWNER,'pid':os.getpid(),'boot_id':boot(),
                       'process_start':Path('/proc/self/stat').read_text().rsplit(') ',1)[1].split()[19],
                       'ports':[PORT],'max_mtu':1500,'transport':'direct OCTEON physical port',
                       'packet_execution':'native-c','hardware_offload':False}
                name='p'+str(PORT)
                link=json.loads(subprocess.check_output(['ip','-n','ffn-data','-j','link','show','dev',name],text=True))[0]
                value['interfaces']={name:link['ifindex']}
                STATE.write_text(json.dumps(value))
            # Only the selected port is attached. Local service permissions are enforced by
            # the kernel INPUT hook; transit Security belongs in FORWARD.
            from ffn_inspection import Inspector
            inspector=Inspector(status_path=None if PORT==1 else Path('/run/ffn-inspection-physical-'+str(PORT)+'.json'))
            if inspector.error:raise RuntimeError('Initial inspection configuration failed: '+inspector.error)
            next_poll=0
            while True:
                if time.monotonic()>=next_poll:
                    next_poll=time.monotonic()+1
                    value['counters']=native.snapshot(inspector)
                    inspector.tick()
                    cfg=json.loads(Path('/etc/ffn/network.json').read_text())
                    native.configure(cfg['ports'].get('p'+str(PORT),{}).get('addresses',[]),inspector)
                    value['updated_at']=time.time()
                    value['updated_monotonic']=time.monotonic()
                    temp=STATE.with_suffix('.tmp');temp.write_text(json.dumps(value));temp.replace(STATE)
                native.poll()
        except KeyboardInterrupt:pass
        finally:
            if native:native.close()
            if inspector:inspector.close()
            STATE.unlink(missing_ok=True)


def execute(action,payload):
    if action=='status' and not payload:return status()
    if action not in ('start','stop') or set(payload)!={'boot_id'} or payload['boot_id']!=boot():
        raise ValueError('Current DP boot identity required')
    if action=='start':
        if status()['running']:return status()
        with open('/run/ffn-fabric.lock','a') as lock, open('/run/ffn-aggregate-port-'+str(PORT)+'.lock','a') as member_owner:
            fcntl.flock(lock,fcntl.LOCK_SH|fcntl.LOCK_NB)
            fcntl.flock(member_owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
            INTENT.write_text(json.dumps({'boot_id':boot(),'front':{str(k):v for k,v in FRONT.items()}}));INTENT.chmod(0o600)
    subprocess.run(['systemctl',action,UNIT],check=True,timeout=20)
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
