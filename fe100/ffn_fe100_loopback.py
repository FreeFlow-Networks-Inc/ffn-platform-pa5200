#!/usr/bin/env python3
"""Temporarily enable NIF Ethernet loopback, then restore it on exit.

Requires an isolated BCM NIF lab return path. This tests the physical Ethernet
path, not parser/flow processing. Send the MP frame probe while READY is shown.
"""
import argparse
import fcntl
import pathlib
import signal
import time
from ffn_fe100 import Fe100, memory_decode_on

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--apply', action='store_true')
p.add_argument('--hold-seconds', type=int, default=20)
args = p.parse_args()
if not 1 <= args.hold_seconds <= 120:
    p.error('hold-seconds must be 1..120')
root = pathlib.Path('/sys/bus/pci/devices/0002:01:00.0')
if (root.joinpath('vendor').read_text().strip() != '0xfeed'
        or root.joinpath('device').read_text().strip() != '0xfe1c'
        or not memory_decode_on()):
    raise SystemExit('FE100 unavailable')
lock = open('/run/ffn-fe100-tables.lock', 'w')
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
fe = Fe100()
original = fe.read32(0x10104)
print('NIF TX configuration: 0x%08x' % original, flush=True)
if not args.apply:
    fe.close()
    raise SystemExit(0)
if original & 8:
    fe.close()
    raise SystemExit('loopback was already enabled; resolve the previous test first')

def interrupted(signum, frame):
    raise SystemExit(128 + signum)

signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGINT, interrupted)
try:
    fe.write32(0x10104, original | 8)
    if not fe.read32(0x10104) & 8:
        raise RuntimeError('loopback enable did not read back')
    print('LOOPBACK_READY', flush=True)
    time.sleep(args.hold_seconds)
finally:
    fe.write32(0x10104, fe.read32(0x10104) & ~8)
    restored = fe.read32(0x10104)
    fe.close()
    print('LOOPBACK_RESTORED 0x%08x' % restored, flush=True)
    if restored & 8:
        raise RuntimeError('loopback did not clear')
