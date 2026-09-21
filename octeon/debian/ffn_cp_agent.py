#!/usr/bin/env python3
"""CP observations for controld; uses the existing BCM and FE100 owners only."""
import argparse
import ast
import asyncio
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import sys
import time

sys.path.insert(0, '/usr/local/lib/ffn')


def fe100_driver_status(root=Path('/')):
    """Read driver metadata only. Never bind, load, enable or map hardware.

    The commissioned FE100 access driver is userspace ffn_fe100.py, not a
    PCI kernel module. The existing non-clearing reader proves access later
    in this same agent observation; file presence alone cannot do that.
    """
    def path(name):
        return root / name.lstrip('/')
    result = {'schema': 1, 'available': True, 'devices': [], 'errors': [],
              'userspace': {'name': 'ffn_fe100.py', 'access': 'devmem-mmio',
                            'installed': False, 'target_pci': None,
                            'sha256': None, 'read_verified': False,
                            'state': 'unavailable'}}
    userspace = result['userspace']
    try:
        driver = path('/usr/local/sbin/ffn_fe100.py')
        with driver.open('rb') as stream:
            source = stream.read(262145)
        if len(source) > 262144:
            raise ValueError('oversized driver')
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(n, ast.Name) and n.id == 'PCI_DEV' for n in node.targets):
                target = ast.literal_eval(node.value)
                if isinstance(target, str):
                    userspace['target_pci'] = target
        userspace.update(installed=True, sha256=hashlib.sha256(source).hexdigest(), state='installed-unverified')
    except (OSError, ValueError, SyntaxError):
        result['errors'].append('Userspace driver metadata unavailable')
    userspace['reader_installed'] = path('/usr/local/sbin/ffn_fe100_lookup_health.py').is_file()
    userspace['register_map_installed'] = path('/opt/ffn-compat/opt/ffn/fe100-csr.json').is_file()
    userspace['memory_device_present'] = path('/dev/mem').exists()
    try:
        entries = list(path('/sys/bus/pci/devices').iterdir())
    except OSError:
        result['available'] = False
        result['errors'].append('CP PCI inventory unavailable')
        return result
    for entry in entries:
        try:
            vendor = entry.joinpath('vendor').read_text().strip().lower()
            device = entry.joinpath('device').read_text().strip().lower()
            if (vendor, device) != ('0xfeed', '0xfe1c'):
                continue
        except OSError:
            result['available'] = False
            result['errors'].append('A PCI identity could not be read')
            continue
        row = {'pci': entry.name, 'model': 'FE100', 'kernel_state': 'unknown',
               'kernel_driver': None, 'kernel_module': None, 'kernel_version': None,
               'bar0_bytes': None, 'memory_decode': None}
        try:
            row['kernel_driver'] = entry.joinpath('driver').readlink().name
            row['kernel_state'] = 'bound'
            try:
                row['kernel_module'] = entry.joinpath('driver/module').readlink().name
                version = entry.joinpath('driver/module/version')
                if version.is_file(): row['kernel_version'] = version.read_text().strip()[:128]
            except OSError:
                pass  # Built-in drivers need not have module metadata.
        except FileNotFoundError:
            row['kernel_state'] = 'unbound'
        except OSError:
            result['errors'].append('Kernel binding could not be read')
        try:
            with entry.joinpath('resource').open() as stream:
                start, end, flags = (int(v, 16) for v in stream.readline().split())
            row['bar0_bytes'] = end - start + 1 if start and end >= start and flags & 0x200 else 0
            with entry.joinpath('config').open('rb') as stream:
                header = stream.read(6)
            if len(header) != 6: raise ValueError('short PCI config')
            row['memory_decode'] = bool(int.from_bytes(header[4:6], 'little') & 2)
        except (OSError, ValueError):
            result['errors'].append('PCI access prerequisites unavailable')
        result['devices'].append(row)
    result['errors'] = list(dict.fromkeys(result['errors']))
    return result


def qualify_fe100_access(driver, telemetry):
    """Tie successful CSR sampling to the actual driver's configured PCI target."""
    userspace = driver.get('userspace') or {}
    target = next((d for d in driver.get('devices', []) if d['pci'] == userspace.get('target_pci')), None)
    verified = bool(driver.get('available') and target and target['memory_decode'] is True
                    and target['bar0_bytes'] == 0x100000 and userspace.get('installed')
                    and userspace.get('reader_installed') and userspace.get('register_map_installed')
                    and userspace.get('memory_device_present') and telemetry.get('available') is True)
    userspace['read_verified'] = verified
    userspace['state'] = 'responding' if verified else 'installed-unverified' if userspace.get('installed') else 'unavailable'
    driver['userspace'] = userspace
    driver['forwarding_verified'] = False
    return driver


def bcm_status():
    with socket.create_connection(('127.1.1.2', 8104), timeout=4) as conn:
        conn.settimeout(5)
        conn.sendall(b'{"op":"port.list"}\n')
        with conn.makefile('rb') as stream:
            raw = stream.readline(131073)
    if len(raw) > 131072 or not raw.endswith(b'\n'):
        raise ValueError('invalid BCM response')
    result = json.loads(raw)
    if result.get('ok') is not True or not isinstance(result.get('ports'), list):
        raise ValueError('BCM owner unavailable')
    return {'available': True, 'ports': result['ports']}


def fe100_status():
    # This existing reader selects only non-clearing aliases and takes the
    # FE100 table lock. Keep the large register dump local to the CP.
    from ffn_planed import process
    observed = asyncio.run(process([sys.executable, '/usr/local/sbin/ffn_fe100_lookup_health.py',
                               '--samples', '2', '--interval', '0.05'], b'', 10))
    last = observed['samples'][-1]['registers']
    return {'available': True, 'summary': observed['summary'],
            'counters': {name: value['raw'] for name, value in last.items()
                         if name.endswith('_stats_ctr_no_rd_clr')},
            'sample_interval_seconds': observed['samples'][-1]['monotonic_time'] -
                                       observed['samples'][0]['monotonic_time'],
            'offload_verified': False}


def policy_recovery_status(path, revision, count, phase):
    """Only a current-boot, recent drain matching the journal is healthy."""
    try:
        report = json.loads(Path(path).read_text())
        age = time.monotonic() - report['monotonic_time']
        fresh = (report.get('schema') == 1 and 0 <= age <= 90 and
                 report.get('cp_boot_id') == Path('/proc/sys/kernel/random/boot_id').read_text().strip())
        return {'available': True, 'fresh': fresh, 'age_seconds': max(0, age),
                'outcome': report.get('outcome'), 'error': report.get('error'),
                'drain_verified': fresh and report.get('outcome') == 'drained' and
                    report.get('revision') == revision and report.get('sessions') == 0 and
                    count == 0 and phase == 'blocked',
                'hardware_activation_verified': False}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {'available': False, 'fresh': False, 'drain_verified': False,
                'hardware_activation_verified': False}


def policy_status(path='/var/lib/ffn/fe100/policy-sessions.sqlite3'):
    """Read one consistent journal snapshot; persisted intent is not activation."""
    db = sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        db.execute('BEGIN')
        row = db.execute('SELECT body FROM policy_state WHERE id=1').fetchone()
        state = json.loads(row[0]) if row else {}
        count = db.execute('SELECT count(*) FROM sessions').fetchone()[0]
        return {'available': True, 'source': 'session intent journal',
                'configured_revision': state.get('revision'), 'configured_phase': state.get('phase'),
                'journaled_sessions': count, 'hardware_activation_verified': False,
                'recovery': policy_recovery_status(Path(path).with_name('policy-recovery.json'),
                    state.get('revision'), count, state.get('phase'))}
    finally:
        db.close()


def snapshot():
    cpu = Path('/proc/cpuinfo').read_text().lower()
    report = {'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
              'ready': False, 'observed_at': time.time(), 'octeon': 'octeon' in cpu,
              'forwarding_verified': False}
    # Failure of one observer must not hide the other subsystem's health.
    for name, observer in (('bcm', bcm_status), ('fe100_driver', fe100_driver_status),
                           ('fe100', fe100_status), ('policy', policy_status)):
        try:
            report[name] = observer()
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error, asyncio.TimeoutError):
            report[name] = {'available': False, 'error': name + ' observation unavailable'}
    report['fe100_driver'] = qualify_fe100_access(report['fe100_driver'], report['fe100'])
    report['ready'] = report['octeon'] and report['bcm']['available']
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('stream', 'status'))
    args = parser.parse_args()
    if args.action == 'stream':
        from ffn_agent_protocol import serve
        serve(snapshot, 'cp', 'pa5200')
    else:
        print(json.dumps(snapshot()))
