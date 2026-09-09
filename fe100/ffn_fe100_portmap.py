#!/usr/bin/env python3
"""Bounded lab programming of one FE100 NIF receive port-map entry."""
import argparse
import ctypes as C
import fcntl
import hashlib
import json
import os
from ffn_fe100 import bar0_base_and_size, memory_decode_on

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--apply', action='store_true')
p.add_argument('--port', type=int, default=8)
p.add_argument('--device', type=int, default=0)
p.add_argument('--swdev', type=int, default=0)
p.add_argument('--lpid', type=int, default=8)
args = p.parse_args()
if not (0 <= args.port <= 255 and 0 <= args.device <= 31 and args.swdev in (0,1) and 0 <= args.lpid <= 255):
    p.error('invalid port-map address')
lock = open('/run/ffn-fe100-tables.lock', 'w')
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
library = '/opt/ffn-compat/tmp/dpfs/usr/local/lib64/libpandp_cp.so.1.0'
with open(library, 'rb') as source:
    if hashlib.file_digest(source, 'sha256').hexdigest() != 'b57227a460144c8c2545fc2e268b31f475ef72ac2a6f1d457387ab46d842c3e9':
        raise SystemExit('owner ABI changed')
base,size = bar0_base_and_size()
if size != 0x100000 or not memory_decode_on(): raise SystemExit('FE100 BAR unavailable')
shim = C.CDLL('/usr/local/lib/ffn/libffn-fe100-tables.so',mode=os.RTLD_GLOBAL|os.RTLD_NOW)
shim.ffn_fe100_open.argtypes = [C.c_uint64,C.c_char_p,C.c_int]
if shim.ffn_fe100_open(base,b'/var/lib/ffn/fe100/portmap-trace.txt',args.apply): raise SystemExit('map failed')
for r in json.load(open('/opt/ffn-compat/opt/ffn/fe100-csr.json')): shim.ffn_fe100_allow(r['addr'])
lib = C.CDLL(library,mode=os.RTLD_LOCAL|os.RTLD_LAZY)
# DWARF pan_fe100_portmap_entry_t is exactly three packed uint8 fields.
entry = (C.c_uint8*3)(args.swdev,args.device,args.port)
fn = lib.pan_fe100_set_rx_portmap_entry
fn.argtypes = [C.c_uint32,C.c_void_p,C.c_int]
fn.restype = C.c_int
result = fn(0,entry,args.lpid)
print('portmap result=%d swdev=%d device=%d port=%d lpid=%d apply=%s' %
      (result,args.swdev,args.device,args.port,args.lpid,args.apply),flush=True)
raise SystemExit(0 if result == 0 else 1)
