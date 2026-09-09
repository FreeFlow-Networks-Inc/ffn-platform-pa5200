#!/usr/bin/env python3
"""Apply and read back the owner's valid system-port mappings for one slot.

Default slot 0 is the isolated lab configuration. The initialized owner
fe100_cfg1 has four priority maps at offsets 1336..1399; entries 255 are
unconfigured and are left untouched. No forwarding policy is created here.
"""
import argparse
import ctypes as C
import fcntl
import hashlib
import json
import os
from ffn_fe100 import bar0_base_and_size, memory_decode_on

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--apply', action='store_true')
p.add_argument('--slot', type=int, default=0)
args = p.parse_args()
if not 0 <= args.slot < 16:
    p.error('slot must be 0..15')
lock = open('/run/ffn-fe100-tables.lock', 'w')
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
library = '/opt/ffn-compat/tmp/dpfs/usr/local/lib64/libpandp_cp.so.1.0'
with open(library, 'rb') as f:
    if hashlib.file_digest(f, 'sha256').hexdigest() != 'b57227a460144c8c2545fc2e268b31f475ef72ac2a6f1d457387ab46d842c3e9':
        raise SystemExit('owner ABI changed')
base, size = bar0_base_and_size()
if size != 0x100000 or not memory_decode_on():
    raise SystemExit('FE100 BAR unavailable')
shim = C.CDLL('/usr/local/lib/ffn/libffn-fe100-tables.so', mode=os.RTLD_GLOBAL | os.RTLD_NOW)
shim.ffn_fe100_open.argtypes = [C.c_uint64, C.c_char_p, C.c_int]
if shim.ffn_fe100_select_block(0x70000):
    raise SystemExit('block selection failed')
if shim.ffn_fe100_open(base, b'/var/lib/ffn/fe100/spm-trace.txt', args.apply):
    raise SystemExit('map failed')
for r in json.load(open('/opt/ffn-compat/opt/ffn/fe100-csr.json')):
    shim.ffn_fe100_allow(r['addr'])
lib = C.CDLL(library, mode=os.RTLD_LOCAL | os.RTLD_LAZY)
defaults = bytes((C.c_char * 2812).in_dll(lib, 'fe100_cfg1'))
put = lib.pan_fe100_set_spm_entry
get = lib.pan_fe100_fetch_spm_entry
for fn in (put, get):
    fn.argtypes = [C.c_uint32, C.c_void_p]
    fn.restype = C.c_int
rows = []
for priority in range(4):
    for port in range(16):
        mapped = defaults[1336 + priority*16 + port]
        if mapped == 255:
            continue
        if mapped > 15:
            raise SystemExit('unexpected owner SPM value')
        # Owner DWARF: 2-byte BE bitfield, slot[13:10], isp[9:6],
        # priority[5:4], mapped system port[3:0].
        key = (args.slot << 10) | (port << 6) | (priority << 4)
        entry = (C.c_ubyte * 2).from_buffer_copy((key | mapped).to_bytes(2, 'big'))
        if args.apply:
            rc = put(0, entry)
            if rc:
                raise SystemExit('SPM set returned %d' % rc)
        rc = get(0, entry)
        if rc:
            raise SystemExit('SPM get returned %d' % rc)
        actual = int.from_bytes(bytes(entry), 'big') & 15
        if args.apply and actual != mapped:
            raise SystemExit('SPM readback differs')
        rows.append({'port': port, 'priority': priority, 'expected': mapped, 'actual': actual})
print(json.dumps({'slot': args.slot, 'apply': args.apply, 'mappings': rows}, indent=2))
