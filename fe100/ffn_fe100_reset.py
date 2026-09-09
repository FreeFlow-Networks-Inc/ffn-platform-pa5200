#!/usr/bin/env python3
"""Lab-only FE100 soft reset; quiesce BCM traffic and initialize again afterward.

Exact two-write sequence from the owner's fe100_reset, at 0x1029ff80.
Does not reset CP, DP, or BCM. Default displays current state without writing.
"""
import argparse
import pathlib
import time
from ffn_fe100 import Fe100, memory_decode_on

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--apply',action='store_true')
args=p.parse_args()
root=pathlib.Path('/sys/bus/pci/devices/0002:01:00.0')
if root.joinpath('vendor').read_text().strip()!='0xfeed' or root.joinpath('device').read_text().strip()!='0xfe1c' or not memory_decode_on():
    raise SystemExit('FE100 PCI identity/decode mismatch')
fe=Fe100()
try:
    value=fe.read32(0xf8150)
    print('FE100 reset control: 0x%08x'%value,flush=True)
    if args.apply:
        fe.write32(0xf8150,value|1)
        fe.write32(0xf8150,value&~1)
        time.sleep(0.1)
        print('Soft reset applied; packet path requires initialization',flush=True)
    for off in (0x10010,0x18204,0x70500):print(hex(off),hex(fe.read32(off)))
finally:fe.close()
