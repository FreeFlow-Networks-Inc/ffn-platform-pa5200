#!/usr/bin/env python3
"""CP port-link observation service replacing the disabled legacy IRQ module.

Reads the existing BCM owner API; never accesses registers or changes ports.
This is a new FFN observation interface, not the vendor ksysd ABI.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import socket
import time
import uuid

STATE = Path('/run/ffn-port-events/state.json')
MAP = (28,13,14,15,16,1,18,19,6,21,22,23,7,11,36,27,10,29,30,31,32,33,34,35)
MAX_RESPONSE = 256 * 1024


def require_cp(device=Path('/sys/bus/pci/devices/0001:01:00.0')):
    if ((device/'vendor').read_text().strip(), (device/'device').read_text().strip()) != ('0x14e4', '0x8375'):
        raise ValueError('PA-5200 CP BCM8375 owner required')


def unknown():
    return [{'port': i+1, 'bcm_port': bcm, 'admin_enabled': None, 'link': None,
             'carrier': None, 'speed_mbps': None} for i,bcm in enumerate(MAP)]


def normalize(response):
    if not isinstance(response, dict) or response.get('ok') is not True or not isinstance(response.get('ports'), list):
        raise ValueError('BCM port inventory unavailable')
    by_id = {}
    for row in response['ports']:
        if not isinstance(row, dict) or type(row.get('port')) is not int or row['port'] in by_id:
            raise ValueError('invalid or duplicate BCM port')
        by_id[row['port']] = row
    result = unknown()
    for port in result:
        row = by_id.get(port['bcm_port'])
        if row is None:
            continue
        if type(row.get('enabled')) is not bool or type(row.get('link')) is not bool:
            raise ValueError('invalid port admin or link observation')
        carrier = row['enabled'] and row['link']
        speed = row.get('speed_mb')
        if carrier and (type(speed) is not int or speed <= 0):
            raise ValueError('linked port speed unavailable')
        port.update(admin_enabled=row['enabled'], link=row['link'], carrier=carrier,
                    speed_mbps=speed if carrier else None)
    return result


def query():
    deadline = time.monotonic() + 2
    with socket.create_connection(('127.1.1.2',8104), timeout=2) as sock:
        sock.settimeout(max(0.001, deadline-time.monotonic()))
        sock.sendall(b'{"op":"port.list"}\n')
        data = bytearray()
        while b'\n' not in data and len(data) <= MAX_RESPONSE:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('BCM observation deadline exceeded')
            sock.settimeout(remaining)
            chunk = sock.recv(min(4096, MAX_RESPONSE+1-len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > MAX_RESPONSE or not data.endswith(b'\n'):
            raise ValueError('oversized or incomplete BCM response')
        return normalize(json.loads(data))


def sample(previous, ports, error, generation, boot_id, now):
    old = {p['port']:p for p in previous['ports']} if previous else {}
    changed = [p['port'] for p in ports if old.get(p['port']) != p]
    transition = previous is None or changed or previous['error'] != error
    sequence = (previous['sequence'] if previous else 0) + bool(transition)
    return {'schema':1, 'role':'control', 'generation':generation, 'boot_id':boot_id,
            'sequence':sequence, 'sample_monotonic':now, 'observed_at':time.time(),
            'valid_for_seconds':6, 'error':error, 'ports':ports,
            'changed_ports':changed, 'poll_interval_seconds':2,
            'lacp_transport_qualified':False}


def qualify(state, boot_id, now):
    age = now - state['sample_monotonic']
    stale = state['boot_id'] != boot_id or not 0 <= age <= state['valid_for_seconds']
    result = dict(state, age_seconds=max(0,age), stale=stale)
    if stale:
        result.update(ports=unknown(), error='port observations expired')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('serve','status','probe'))
    args=parser.parse_args()
    boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if args.action == 'status':
        state=json.loads(STATE.read_text())
        print(json.dumps(qualify(state,boot_id,time.monotonic()),indent=2)); return
    require_cp()
    if args.action == 'probe':
        print(json.dumps(sample(None,query(),None,str(uuid.uuid4()),boot_id,time.monotonic()),indent=2)); return
    os.umask(0o077)
    STATE.parent.mkdir(parents=True,exist_ok=True)
    with (STATE.parent/'lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        generation=str(uuid.uuid4()); previous=None
        while True:
            start=time.monotonic()
            try: ports,error=query(),None
            except (OSError,ValueError,TypeError): ports,error=unknown(),'BCM observation failed'
            state=sample(previous,ports,error,generation,boot_id,time.monotonic())
            tmp=STATE.with_suffix('.tmp')
            tmp.write_text(json.dumps(state)+'\n'); tmp.replace(STATE)
            previous=state
            time.sleep(max(0.1,2-(time.monotonic()-start)))


if __name__ == '__main__': main()
