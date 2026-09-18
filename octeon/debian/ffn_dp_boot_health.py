#!/usr/bin/env python3
"""Read-only DP boot qualification, including initramfs/chroot distinction."""
import os
from pathlib import Path


def os_id(path):
    for line in path.read_text().splitlines():
        if line.startswith('ID='):
            return line[3:].strip().strip('"\'')
    return None


def inspect_boot(proc=Path('/proc'), root=Path('/')):
    result = {'ready': False, 'process_os': None, 'pid1_os': None,
              'pid1_executable': None, 'same_root_as_pid1': False, 'reasons': []}
    try:
        result['process_os'] = os_id(root / 'etc/os-release')
    except OSError:
        result['reasons'].append('current process has no readable OS identity')
    try:
        result['pid1_os'] = os_id(proc / '1/root/etc/os-release')
    except OSError:
        result['reasons'].append('PID 1 root OS identity is unavailable')
    try:
        result['pid1_executable'] = os.readlink(proc / '1/exe')
    except OSError:
        result['reasons'].append('PID 1 executable is unavailable')
    try:
        a, b = root.stat(), (proc / '1/root').stat()
        result['same_root_as_pid1'] = (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
    except OSError:
        result['reasons'].append('root identity comparison failed')
    if result['process_os'] != 'debian' or result['pid1_os'] != 'debian':
        result['reasons'].append('Debian userspace is not active for both the agent and PID 1')
    if not result['same_root_as_pid1']:
        result['reasons'].append('agent is in a different root from PID 1')
    if result['pid1_executable'] not in ('/usr/lib/systemd/systemd', '/lib/systemd/systemd'):
        result['reasons'].append('PID 1 has not handed control to systemd')
    result['ready'] = not result['reasons']
    return result
