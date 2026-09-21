#!/usr/bin/env python3
"""Explicit, serialized PA-5220 dataplane packet initialization stages."""
import argparse
import fcntl
import json
import os
import subprocess
import uuid
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


def reconcile(expected_boot, root=ROOT, boot_id=None, boot_check=None,
              loader=subprocess.run, read=None, stage=None, start=None,
              lock_path='/run/ffn-packet-reconcile.lock',link=None):
    """Restore missing stages once; never reset a running or faulted engine."""
    if str(uuid.UUID(expected_boot))!=expected_boot:raise ValueError('Current DP boot identity required')
    if boot_id is None:boot_id=lambda:Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if boot_check is None:
        from ffn_dp_boot_health import inspect_boot
        boot_check=inspect_boot
    if read is None:read=lambda:status(root)
    if stage is None:stage=lambda op:prepare(root,operation=op)
    if start is None:start=lambda:set_trunk(True,root)
    def fence():
        if boot_id()!=expected_boot:raise RuntimeError('DP lifetime changed during fabric recovery')
    with open(lock_path,'a') as owner:
        fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
        fence()
        if boot_check().get('ready') is not True:raise RuntimeError('DP boot incomplete')
        if not (root/'status').exists():
            loader(['modprobe','ffn_dp_packet_init'],check=True,capture_output=True,text=True,timeout=15)
        packet=read();changed=[]
        if packet.get('dma_error') or packet.get('trunk',{}).get('error') or packet.get('pki_reset_busy'):
            raise RuntimeError('Packet fabric fault requires recovery; automatic reset refused')
        if link is None:
            from ffn_dp_link import ensure
            link=ensure
        fence()
        if link().get('internal_link_ready') is not True:raise RuntimeError('Internal packet link not acknowledged')
        for operation,flag in STAGES.items():
            fence()
            if packet.get(flag) is True:continue
            if any(packet.get(k)!=0 for k in ('pki_active','pki_enabled','pko_enabled')):
                raise RuntimeError('Cannot initialize missing stages on an active packet engine')
            stage(operation);packet=read();fence()
            if packet.get(flag) is not True:raise RuntimeError('Packet stage not acknowledged: '+operation)
            changed.append(operation)
        if packet.get('trunk',{}).get('running') is not True:
            fence();start();changed.append('start-trunk')
        packet=read();fence();trunk=packet.get('trunk',{})
        if (packet.get('dma_error') or trunk.get('error') or trunk.get('running') is not True
            or trunk.get('dq_open') is not True or packet.get('pki_enabled')!=1 or packet.get('pko_enabled')!=1):
            raise RuntimeError('Packet fabric runtime not acknowledged')
        return dict(ready=True,boot_id=expected_boot,changed=changed,packet_initialization=packet)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('status', 'reconcile', 'start-trunk', 'stop-trunk', *STAGES))
    args = parser.parse_args()
    if args.operation == 'reconcile':
        import sys
        request=json.load(sys.stdin)
        if not isinstance(request,dict) or set(request)!={'boot_id'}:raise ValueError('Expected boot_id only')
        result=reconcile(request['boot_id'])
    elif args.operation == 'status': result = status()
    elif args.operation in ('start-trunk','stop-trunk'):
        result = set_trunk(args.operation == 'start-trunk')
    else: result = prepare(operation=args.operation)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    try:main()
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as error:
        print(json.dumps({'error':str(error)}));raise SystemExit(1)
