#!/usr/bin/env python3
"""Read-only CP hardware verification. This never qualifies production offload."""
import json
import os
from pathlib import Path
import sys
import time


def evaluate(snapshot):
    blockers = []
    if snapshot.get('error'): blockers.append(snapshot['error'])
    if snapshot.get('fe100_pci_driver') != 'ffn_fe100':
        blockers.append('FE100 PCI driver is not bound')
    if snapshot.get('pid1') not in ('/usr/lib/systemd/systemd', '/lib/systemd/systemd'):
        blockers.append('Native systemd CP is not running')
    bcm = snapshot.get('bcm', {})
    if bcm.get('state') != 'ready' or bcm.get('init_errors'):
        blockers.append('BCM initialization is not ready')
    ports = {p['port']: p for p in snapshot.get('ports', [])}
    for port in (3, 20, 24):
        if not ports.get(port, {}).get('link'):
            blockers.append('Internal BCM link '+str(port)+' is down')
    copper = {p['phy']: p for p in snapshot.get('copper', [])}
    for address in range(16, 20):
        row = copper.get(address, {})
        if row.get('id') != [0x600d, 0x84f9] or row.get('firmware') != 0x1089 or row.get('reset'):
            blockers.append('Copper PHY '+str(address)+' identity/firmware is unverified')
    if snapshot.get('mdio_write_gates') != {'allow_writes': False, 'allow_gearbox_writes': False}:
        blockers.append('MDIO write gates are not both closed')
    session = snapshot.get('fe100', {})
    if session.get('cp_boot_id') != snapshot.get('cp_boot_id'):
        blockers.append('FE100 evidence does not match this CP boot')
    if not session.get('initialized'):
        blockers.append('FE100 lookup memory is not initialized')
    blockers.extend(session.get('blockers', []))
    blockers.extend(session.get('action_blockers', []))
    generation = snapshot.get('hardware_generation')
    table = snapshot.get('session_validation', {})
    table_verified = (not blockers and isinstance(generation, dict)
        and bool(generation.get('epoch'))
        and generation.get('cp_boot_id') == snapshot.get('cp_boot_id')
        and table.get('hardware_generation') == generation
        and table.get('cp_boot_id') == snapshot.get('cp_boot_id')
        and table.get('stage') == 'completed'
        and table.get('session_table_verified') is True
        and table.get('cleanup_verified') is True and table.get('faults') == 0)
    return {'schema': 1, 'scope': 'CP hardware initialization and internal carrier',
            'sampled_at': time.time(), 'snapshot': snapshot,
            'initialization_verified': not blockers,
            'session_table_verified': table_verified,
            'blockers': list(dict.fromkeys(blockers)),
            'physical_forwarding_verified': False, 'production_offload_qualified': False,
            'remaining': ([] if table_verified else ['Session-table lifecycle and cleanup in this hardware generation']) +
                         ['Physical packet/NAT qualification in this boot',
                          'Production policy and session-admission qualification']}


def collect():
    from ffn_faceplate import call
    from ffn_mdio import Mdio
    from ffn_fe100_live_sessions import LiveSessions
    from ffn_fe100 import bound_resource_path, SYSFS
    result = {'cp_boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
              'pid1': os.readlink('/proc/1/exe'), 'kernel_release': os.uname().release}
    result['fe100_resource'] = bound_resource_path()
    result['fe100_pci_driver'] = (Path(SYSFS)/'driver').resolve().name
    result['bcm'] = call({'op': 'status'})
    result['ports'] = call({'op': 'port.list'})['ports']
    # The same lock as the firmware/configuration owners prevents interleaving.
    import fcntl
    with open('/run/lock/ffn-copper.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        bus = Mdio()
        try:
            result['copper'] = [{'phy': p, 'id': [bus.transfer(p,1,2), bus.transfer(p,1,3)],
                'firmware': bus.transfer(p,30,0x400f), 'reset': bool(bus.transfer(p,1,0)&0x8000),
                'enabled': not bool(bus.transfer(p,30,0x401a)&0x8180)} for p in range(16,20)]
        finally: bus.close()
    result['mdio_write_gates'] = {name: Path('/sys/module/ffn_mdioctl/parameters', name).read_text().strip() in ('1','Y')
                                 for name in ('allow_writes','allow_gearbox_writes')}
    result['fe100'] = LiveSessions(False).status()
    root = Path('/var/lib/ffn/fe100')
    generation = root/'generation.json'
    result['hardware_generation'] = json.loads(generation.read_text()) if generation.exists() else None
    journals = list(root.glob('session-validation-*.json'))
    if journals:
        latest = max(journals, key=lambda p: p.stat().st_mtime_ns)
        # Invalid latest evidence must fail the check, never select an older pass.
        result['session_validation'] = json.loads(latest.read_text())
    return result


if __name__ == '__main__':
    try: report = evaluate(collect())
    except Exception as error: report = evaluate({'error': str(error)})
    print(json.dumps(report, indent=2))
    sys.exit(0 if report['initialization_verified'] else 1)
