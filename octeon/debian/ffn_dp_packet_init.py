#!/usr/bin/env python3
"""Explicit, serialized PA-5220 dataplane packet initialization stages."""
import argparse
import fcntl
import json
import os
import subprocess
from pathlib import Path

ROOT = Path('/sys/kernel/debug/ffn_dp_packet_init')


def status(root=ROOT):
    with (root/'status').open() as source:
        data = source.read(4097)
    if len(data) > 4096:
        raise RuntimeError('oversized packet-init status')
    value = json.loads(data)
    if value.get('schema') != 1 or value.get('ready') is not False:
        raise RuntimeError('unexpected packet-init ABI')
    return value


STAGES = {'prepare-pki':'pki_microcode_prepared', 'prepare-dma':'dma_ready',
          'prepare-sso':'sso_xaq_prepared', 'prepare-pko-memory':'pko_memory_ready',
          'prepare-pko-queues':'pko_queues_prepared', 'prepare-trunk':'trunk_registered'}


def prepare(root=ROOT, lock_path='/run/ffn-fabric.lock', boot_check=None,
            operation='prepare-pki'):
    if operation not in STAGES:
        raise ValueError('unknown initialization stage')
    if boot_check is None:
        from ffn_dp_boot_health import inspect_boot
        boot_check = inspect_boot
    with open(lock_path, 'a') as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if boot_check().get('ready') is not True:
            raise RuntimeError('DP Debian boot incomplete')
        before = status(root)
        if (before.get('pki_reset_busy') is not False or
                any(before.get(k) != 0 for k in ('pki_active', 'pki_enabled', 'pko_enabled'))):
            raise RuntimeError('packet engines are not quiescent')
        if operation != 'prepare-pki' and before.get('dma_error', 0):
            raise RuntimeError('DMA initialization failed; recovery requires a DP reboot')
        if operation in ('prepare-sso','prepare-pko-memory','prepare-pko-queues') and before.get('dma_ready') is not True:
            raise RuntimeError('verified DMA pools required before queue memory')
        if operation == 'prepare-pko-queues' and before.get('pko_memory_ready') is not True:
            raise RuntimeError('verified PKO memory required before queues')
        if operation == 'prepare-trunk' and any(before.get(k) is not True for k in
                ('dma_ready', 'sso_xaq_prepared', 'pki_microcode_prepared', 'pko_queues_prepared')):
            raise RuntimeError('all packet initialization stages required before trunk')
        # Exactly one write: buffered text I/O may retry a failed sysfs/debugfs
        # command during close, hiding the first hardware error.
        command = os.open(root/'prepare', os.O_WRONLY)
        try:
            payload = (operation+'\n').encode('ascii')
            if os.write(command, payload) != len(payload):
                raise RuntimeError('short initialization command write')
        finally:
            os.close(command)
        after = status(root)
        if after.get(STAGES[operation]) is not True or after.get('pki_enabled') != 0:
            raise RuntimeError(operation+' not verified')
        return after


def set_trunk(enabled, root=ROOT, lock_path='/run/ffn-fabric.lock', runner=subprocess.run):
    """Explicit MP-controlled activation; no front-port assignments are changed."""
    with open(lock_path, 'a') as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = status(root)
        t = before.get('trunk', {})
        if t.get('interface') != 'ffnpkt0':
            raise RuntimeError('registered FFN packet trunk required')
        if enabled and (t.get('error') or before.get('dma_error')):
            raise RuntimeError('packet runtime fault; DP reset required')
        runner(['ip', 'link', 'set', 'dev', 'ffnpkt0', 'up' if enabled else 'down'],
               check=True, timeout=15)
        after = status(root)
        t = after.get('trunk', {})
        if t.get('running') is not enabled or t.get('error'):
            raise RuntimeError('packet trunk transition not verified: '+json.dumps(t))
        return after


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('status', 'start-trunk', 'stop-trunk', *STAGES))
    args = parser.parse_args()
    if args.operation == 'status': result = status()
    elif args.operation in ('start-trunk','stop-trunk'):
        result = set_trunk(args.operation == 'start-trunk')
    else: result = prepare(operation=args.operation)
    print(json.dumps(result, indent=2))


if __name__ == '__main__': main()
