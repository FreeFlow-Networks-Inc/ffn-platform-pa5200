#!/usr/bin/env python3
"""PA-5220 BGX2 internal 40G link commissioning; never enables PKI or DMA."""
import argparse
import fcntl
import json
from pathlib import Path
import time

STATUS = Path('/sys/kernel/debug/ffn_dp_link/status')
ENABLE = Path('/sys/kernel/debug/ffn_bgx/enable')


def validate(state):
    if (state.get('schema'), state.get('bgx'), state.get('lmac'), state.get('lmac_type')) != (1,2,0,4):
        raise ValueError('expected BGX2 LMAC0 XLAUI hardware')
    for field in ('enabled','rx_enabled','tx_enabled','link','block_lock','fault','pki_enabled'):
        if type(state.get(field)) is not int or state[field] not in (0,1):
            raise ValueError('invalid '+field+' observation')
    if type(state.get('pknd')) is not int or not 0 <= state['pknd'] <= 63:
        raise ValueError('invalid port-kind observation')


def report(state):
    validate(state)
    ready = all(state[k] == 1 for k in ('enabled','rx_enabled','tx_enabled','link','block_lock')) and not state['fault'] and state['pknd']==8
    return {'schema':1,'role':'dataplane','sample_monotonic':time.monotonic(),
            'internal_link_ready':ready, 'physical_packet_transport_verified':False,
            'session_offload_verified':False, 'hardware':state}


def observe(path=STATUS):
    try:
        with path.open() as source:
            raw = source.read(8193)
        if len(raw) > 8192:
            raise ValueError('oversized hardware observation')
        return dict(report(json.loads(raw)), available=True)
    except (OSError, ValueError, TypeError, AttributeError):
        return {'available':False, 'internal_link_ready':None,
                'physical_packet_transport_verified':False, 'session_offload_verified':False,
                'error':'DP link probe unavailable or invalid'}


def enable_once(read, write):
    before = read(); validate(before)
    if before['pki_enabled']:
        raise RuntimeError('packet input is active; refusing link reinitialization')
    if before['enabled']:
        # Never re-key a live LMAC or repeatedly run the SDK initialization.
        return dict(report(before), changed=False)
    if before['rx_enabled'] or before['tx_enabled']:
        raise RuntimeError('unexpected partially enabled link')
    # The existing audited kernel helper assigns pknd=8, then calls the
    # imported SDK's bounded BGX XLAUI bring-up routine for interface 2 only.
    try:
        write('2\n')
    except OSError:
        return dict(report(read()), changed=None, error='kernel link enable failed; inspect observed state')
    after = read()
    result = dict(report(after), changed=True)
    if after['pki_enabled']:
        result.update(error='packet input changed concurrently; stop commissioning', internal_link_ready=False)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('status','enable'))
    args=parser.parse_args()
    read=lambda:json.loads(STATUS.read_text())
    if args.action=='status':
        print(json.dumps(report(read()),indent=2)); return
    from ffn_dp_boot_health import inspect_boot
    if not inspect_boot()['ready']:
        raise RuntimeError('completed DP Debian boot required')
    # Shared with the software relay/direct transport so commissioning cannot
    # overlap those processes. SDK/other kernel owners still need coordination.
    with open('/run/ffn-fabric.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        result=enable_once(read,ENABLE.write_text)
    print(json.dumps(result,indent=2))
    if not result['internal_link_ready'] or result.get('error'):
        raise SystemExit(1)


if __name__=='__main__': main()
