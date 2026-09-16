#!/usr/bin/env python3
"""CP observations for controld; uses the existing BCM and FE100 owners only."""
import argparse
import asyncio
import json
from pathlib import Path
import socket
import sqlite3
import sys
import time

sys.path.insert(0, '/usr/local/lib/ffn')


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
                'journaled_sessions': count, 'hardware_activation_verified': False}
    finally:
        db.close()


def snapshot():
    cpu = Path('/proc/cpuinfo').read_text().lower()
    report = {'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
              'ready': False, 'observed_at': time.time(), 'octeon': 'octeon' in cpu,
              'forwarding_verified': False}
    # Failure of one observer must not hide the other subsystem's health.
    for name, observer in (('bcm', bcm_status), ('fe100', fe100_status), ('policy', policy_status)):
        try:
            report[name] = observer()
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error, asyncio.TimeoutError):
            report[name] = {'available': False, 'error': name + ' observation unavailable'}
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
