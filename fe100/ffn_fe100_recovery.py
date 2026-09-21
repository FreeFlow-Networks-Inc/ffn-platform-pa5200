#!/usr/bin/env python3
"""Bounded CP recovery job. Never admits flows, resets hardware or flushes tables."""
import json
import os
from pathlib import Path
import time
from ffn_fe100_policy_control import control

REPORT = Path('/var/lib/ffn/fe100/policy-recovery.json')


def recover(run=control):
    report = {'schema': 1, 'cp_boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
              'observed_at': time.time(), 'monotonic_time': time.monotonic(),
              'admission_enabled': False, 'hardware_activation_verified': False}
    try:
        state = run('reconcile', {})
        if (state.get('phase') != 'blocked' or state.get('sessions') != 0 or
                state.get('recovery_required') is not False or state.get('admission_enabled') is not False):
            raise RuntimeError('session drain incomplete')
        report.update(outcome='drained', revision=state['revision'], sessions=0)
    except BlockingIOError:
        report.update(outcome='busy', error='Another journal or hardware owner holds the lock')
    except Exception as exc:
        report.update(outcome='blocked', error=str(exc)[:512])
    return report


def publish(report, path=REPORT):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    try:
        with temp.open('w') as stream:
            json.dump(report, stream, sort_keys=True)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        temp.replace(path)
        fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


if __name__ == '__main__':
    result = recover()
    publish(result)
    print(json.dumps(result))
    raise SystemExit(1 if result['outcome'] == 'blocked' else 0)
