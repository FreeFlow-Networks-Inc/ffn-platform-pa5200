#!/usr/bin/env python3
"""Run the owner's standalone gearbox initializer, confined to MDIO bus 1 PHY 0.

Default is a write-denied interposition test. Uses owner runtime in place.
"""
import argparse
import ctypes as C
import hashlib
import os
import pathlib
import signal

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--apply', action='store_true')
p.add_argument('--trace', default='/var/lib/ffn/fe100/gearbox-trace.txt')
args = p.parse_args()
library = '/opt/ffn-compat/tmp/dpfs/usr/local/lib64/libpanbcm_cp.so.1.0'
with open(library, 'rb') as source:
    if hashlib.file_digest(source, 'sha256').hexdigest() != '8e4a786a901903a5806edffc213a0f0727167e5f7b7a8b0a145bf2d94aea2040':
        raise SystemExit('vendor library differs from inspected ABI')
shim = C.CDLL('/usr/local/lib/ffn/libffn-gearbox-mdio.so', mode=os.RTLD_GLOBAL | os.RTLD_NOW)
shim.ffn_gearbox_open.argtypes = [C.c_char_p, C.c_int]
shim.pan_read_gearbox_register.argtypes = [C.c_void_p, C.c_uint32, C.c_uint32, C.POINTER(C.c_uint32)]
if shim.ffn_gearbox_open(args.trace.encode(), args.apply): raise SystemExit('adapter open failed')
ids = []
for reg in (0x10002, 0x10003):
    value = C.c_uint32()
    if shim.pan_read_gearbox_register(None, 0, reg, C.byref(value)): raise SystemExit('MDIO read failed')
    ids.append(value.value)
if ids != [0xae02, 0x5290]: raise SystemExit('unexpected gearbox identity: %r' % ids)
print('Gearbox PHY ID: ae02:5290', flush=True)
lib = C.CDLL(library, mode=os.RTLD_LOCAL | os.RTLD_LAZY)
fn = lib.bcm_gearbox_phy_initialize
fn.argtypes = [C.c_uint32]
fn.restype = C.c_int
gate = pathlib.Path('/sys/module/ffn_mdioctl/parameters/allow_gearbox_writes')
def timed_out(signum, frame):
    raise TimeoutError('gearbox initialization exceeded 180 seconds')
signal.signal(signal.SIGALRM, timed_out)
try:
    if args.apply: gate.write_text('1\n')
    signal.alarm(180)
    result = fn(0)
    print('Gearbox init return: %d; apply=%s' % (result, args.apply), flush=True)
finally:
    signal.alarm(0)
    gate.write_text('0\n')
raise SystemExit(0 if result == 0 else 1)
