#!/usr/bin/env python3
"""Capture one TMI header using the owner's pdt/fe100.py diagnostic sequence.

Select IL_TX=2, EGR=0 or IPQ=1, clear/rearm cap_vld, enable cap_first.
Fault status bits are not written back as ones. Captured words are displayed
in the same big-endian byte order as the owner's diagnostic, independently
of the little-endian MMIO register layout.
"""
import argparse
import fcntl
import json
import signal
import time
from ffn_fe100 import Fe100, memory_decode_on

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--apply', action='store_true')
p.add_argument('--source', choices=('il-tx', 'egr', 'ipq'), default='il-tx')
p.add_argument('--seconds', type=int, default=20)
args = p.parse_args()
if not 1 <= args.seconds <= 120:
    p.error('seconds must be 1..120')
if not memory_decode_on():
    raise SystemExit('FE100 memory decode is off')
lock = open('/run/ffn-fe100-tables.lock', 'w')
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
fe = Fe100()
mode = fe.read32(0x8200)
source = fe.read32(0x8300)
if not args.apply:
    print(json.dumps({'mode': hex(mode), 'source': source}))
    fe.close()
    raise SystemExit(0)
if mode & 8:
    fe.close()
    raise SystemExit('another TMI capture is enabled')

def interrupted(signum, frame):
    raise SystemExit(128 + signum)

signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGINT, interrupted)
try:
    fe.write32(0x8300, {'il-tx': 2, 'egr': 0, 'ipq': 1}[args.source])
    control = mode & ~0x3068
    fe.write32(0x8200, control | 0x20 | 0x40)
    fe.write32(0x8200, control | 0x20 | 8)
    print('CAPTURE_READY', flush=True)
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline and not fe.read32(0x8200) & 0x40:
        time.sleep(0.05)
    valid = bool(fe.read32(0x8200) & 0x40)
    words = [fe.read32(0x8304 + 4*i) for i in range(16)] if valid else []
    print(json.dumps({'source': args.source, 'valid': valid,
                      'words': ['%08x' % v for v in words],
                      'hex': b''.join(v.to_bytes(4, 'big') for v in words).hex()}), flush=True)
finally:
    # Restore capture controls, preserving current non-capture controls.
    current = fe.read32(0x8200)
    fe.write32(0x8200, (current & ~0x3068) | (mode & 0x28))
    fe.write32(0x8300, source)
    fe.close()
    print('CAPTURE_RESTORED', flush=True)
