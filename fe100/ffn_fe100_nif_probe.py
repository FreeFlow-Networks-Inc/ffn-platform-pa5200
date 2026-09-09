#!/usr/bin/env python3
"""CP: run vendor NIF or TMI link initialization through a bounded adapter.

Default denies every write to verify symbol interposition before using --apply.
Each adapter confines accesses to approved registers in its selected block. No PAN
system-state service, full-chip reset, parser load or forwarding setup is called.
"""
import argparse
import ctypes as C
import hashlib
import json
import os
import pathlib
import struct

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--apply', action='store_true')
p.add_argument('--block', choices=('nif', 'tmi'), default='nif')
p.add_argument('--trace', default='/var/lib/ffn/fe100/nif-trace.txt')
args = p.parse_args()
library = '/opt/ffn-compat/tmp/dpfs/usr/local/lib64/libpandp_cp.so.1.0'
with open(library, 'rb') as source:
    if hashlib.file_digest(source, 'sha256').hexdigest() != 'b57227a460144c8c2545fc2e268b31f475ef72ac2a6f1d457387ab46d842c3e9':
        raise SystemExit('vendor library differs from the inspected ABI')
root = pathlib.Path('/sys/bus/pci/devices/0002:01:00.0')
if root.joinpath('vendor').read_text().strip() != '0xfeed' or root.joinpath('device').read_text().strip() != '0xfe1c':
    raise SystemExit('unexpected PCI device')
config = root.joinpath('config').read_bytes()
if not int.from_bytes(config[4:6], 'little') & 2:
    raise SystemExit('FE100 memory decode is off')
base, end, flags = (int(v, 16) for v in root.joinpath('resource').read_text().splitlines()[0].split())
if end - base + 1 != 0x100000: raise SystemExit('unexpected FE100 BAR size')
adapter = 'libffn-fe100-mmio.so' if args.block == 'nif' else 'libffn-fe100-tmi-mmio.so'
shim = C.CDLL('/usr/local/lib/ffn/' + adapter, mode=os.RTLD_GLOBAL | os.RTLD_NOW)
shim.ffn_fe100_open.argtypes = [C.c_uint64, C.c_char_p, C.c_int]
shim.ffn_fe100_allow.argtypes = [C.c_uint32]
shim.fe100_reg_rd.argtypes = [C.c_uint32, C.c_uint32, C.POINTER(C.c_uint32)]
if shim.ffn_fe100_open(base, args.trace.encode(), args.apply) != 0:
    raise SystemExit('cannot map FE100 or open trace')
for entry in json.loads(pathlib.Path('/opt/ffn-compat/opt/ffn/fe100-csr.json').read_text()):
    if shim.ffn_fe100_allow(entry['addr']) != 0: raise SystemExit('invalid register map')
# These NIF SerDes locations are absent from the public CSR descriptor table.
# Each is an explicit address in this owner's pan_fe100_nif_100g_init routine.
for offset in [0x15428, 0x17428, *range(0x14000, 0x15000, 0x100),
               *range(0x16000, 0x16800, 0x100)]:
    if shim.ffn_fe100_allow(offset) != 0: raise SystemExit('invalid SerDes offset')
word = C.c_uint32()
if args.block == 'tmi':
    # Explicit TMI SerDes addresses in the inspected IL initialization routine.
    for offset in [0xd428, 0xf428, *range(0xc000, 0xd000, 0x100),
                   *range(0xe000, 0xe800, 0x100),
                   0xb008, 0xb010, 0xb020, 0xb030, 0xb0c0, 0xb150, 0xb154,
                   0xb180, 0xb184, 0xb208, 0xb210, 0xb230, 0xb2c0, 0xb350,
                   0xb354, 0xb380, 0xb384, 0xb3ac, 0xb3b0, 0xb3b4, 0xb3b8,
                   0xb3bc, 0xb3c0]:
        if shim.ffn_fe100_allow(offset): raise SystemExit('invalid TMI SerDes offset')
if shim.fe100_reg_rd(0, 0x10010 if args.block == 'nif' else 0x8008, C.byref(word)) != 0:
    raise SystemExit('adapter read failed')
print('%s reset control: 0x%08x' % (args.block.upper(), word.value), flush=True)
lib = C.CDLL(library, mode=os.RTLD_LOCAL | os.RTLD_LAZY)
# Verified in this library's DWARF: pan_fe100_cnf_t is 2812 bytes, NIF at
# 2476; NIF's interface/switch/enable fields occupy its first four words.
cfg = C.create_string_buffer(2812)
struct.pack_into('>IIII', cfg, 2476, 2, 2, 1, 1)  # 100G, Qumran, both NIF halves
fn = lib.pan_fe100_nif_100g_init
if args.block == 'tmi':
    # Owner fe100_cfg1 in the hash-pinned ELF: single-channel, in-band FC,
    # enhanced segmentation, 12.5 Gbaud, and the board's physical lane order.
    struct.pack_into('>IIIII', cfg, 2576, 0, 1, 1, 2, 12)
    cfg[2596:2608] = bytes((11, 10, 9, 0, 8, 4, 5, 6, 1, 2, 7, 3))
    struct.pack_into('>IIIIIII', cfg, 2608, 128, 256, 64, 128, 256, 64, 1)
    fn = lib.pan_fe100_tmi_il_init
fn.argtypes = [C.c_uint32, C.c_void_p]
fn.restype = C.c_int
result = fn(0, C.byref(cfg))
print('%s init return: %d; apply=%s; trace=%s' % (args.block.upper(), result, args.apply, args.trace), flush=True)
raise SystemExit(0 if result == 0 else 1)
