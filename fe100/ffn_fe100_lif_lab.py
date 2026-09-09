#!/usr/bin/env python3
"""Inspect or install one port-specific lab LIF entry using the owner driver ABI.

The owner's pdt diagnostic defines forwarding type 5 as SYSPORT. Mask one
bits mean comparison, so only the six-bit ingress logical port is matched.
Default only describes the entry; --inspect issues indirect table reads.
"""
import argparse
import ctypes as C
import fcntl
import hashlib
import json
import os
import struct
from ffn_fe100 import Fe100, bar0_base_and_size, memory_decode_on

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--apply', action='store_true')
p.add_argument('--inspect', action='store_true')
p.add_argument('--port', type=int, default=8)
p.add_argument('--destination', type=int, default=8)
p.add_argument('--index', type=int, default=0)
args = p.parse_args()
if not (0 <= args.port < 64 and 0 <= args.destination < 16 and 0 <= args.index < 32):
    p.error('port must be 0..63, destination 0..15 and lab index 0..31')
# Hash-pinned DWARF: entry size 36, payload words at 4/8/12, mask at
# 16, key at 26; packed 80-bit key has in_pport in bits 37..32.
data = bytearray(36)
struct.pack_into('>III', data, 4, (1 << 31) | (5 << 16), args.destination, args.port << 16)
data[16:26] = (63 << 32).to_bytes(10, 'big')
data[26:36] = (args.port << 32).to_bytes(10, 'big')
print(json.dumps({'index': args.index, 'port': args.port, 'destination': args.destination,
                  'entry': data.hex()}), flush=True)
if not (args.apply or args.inspect):
    raise SystemExit(0)
lock = open('/run/ffn-fe100-tables.lock', 'w')
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
library = '/opt/ffn-compat/tmp/dpfs/usr/local/lib64/libpandp_cp.so.1.0'
with open(library, 'rb') as f:
    if hashlib.file_digest(f, 'sha256').hexdigest() != 'b57227a460144c8c2545fc2e268b31f475ef72ac2a6f1d457387ab46d842c3e9':
        raise SystemExit('owner ABI changed')
base, size = bar0_base_and_size()
if size != 0x100000 or not memory_decode_on():
    raise SystemExit('FE100 unavailable')
fe = Fe100()
table = (fe.read32(0x28008) >> 1) & 1
fe.close()
if table != 0:
    raise SystemExit('lab requires lookup table 0')
shim = C.CDLL('/usr/local/lib/ffn/libffn-fe100-tables.so', mode=os.RTLD_GLOBAL | os.RTLD_NOW)
shim.ffn_fe100_open.argtypes = [C.c_uint64, C.c_char_p, C.c_int]
if shim.ffn_fe100_select_block(0x80000) or shim.ffn_fe100_select_lif_table(table):
    raise SystemExit('adapter selection failed')
# Indirect READ commands also need register writes. Only the owner table
# getter is called unless --apply explicitly requests an entry insertion.
if shim.ffn_fe100_open(base, b'/var/lib/ffn/fe100/lif-lab-trace.txt', 1):
    raise SystemExit('map failed')
for r in json.load(open('/opt/ffn-compat/opt/ffn/fe100-csr.json')):
    shim.ffn_fe100_allow(r['addr'])
lib = C.CDLL(library, mode=os.RTLD_LOCAL | os.RTLD_LAZY)
get = lib.pan_fe100_fetch_lif_entry
put = lib.pan_fe100_insert_lif_entry
for fn in (get, put):
    fn.argtypes = [C.c_uint32, C.c_void_p, C.c_uint32]
    fn.restype = C.c_int
entry = (C.c_ubyte * 36)()
rc = get(0, entry, args.index)
print(json.dumps({'fetch_rc': rc, 'entry': bytes(entry).hex()}), flush=True)
if not args.apply:
    raise SystemExit(0 if rc in (0, 3) else 1)
# Verified with this owner's condor_strerr(3): NOTFOUND.
if rc not in (0, 3):
    raise SystemExit('cannot establish existing entry state')
if entry[4] & 0x80:
    raise SystemExit('lab slot already contains a valid entry; inspect before changing it')
entry = (C.c_ubyte * 36).from_buffer_copy(data)
rc = put(0, entry, args.index)
print('LIF insert return=%d' % rc, flush=True)
if rc:
    raise SystemExit(1)
readback = (C.c_ubyte * 36)()
rc = get(0, readback, args.index)
print(json.dumps({'readback_rc': rc, 'entry': bytes(readback).hex()}), flush=True)
actual = bytes(readback)
if (rc or actual[4:16] != data[4:16]
        or ((int.from_bytes(actual[26:36], 'big') >> 32) & 63) != args.port
        or ((int.from_bytes(actual[16:26], 'big') >> 32) & 63) != 63):
    raise SystemExit('LIF forwarding data or ingress-port match did not read back')
