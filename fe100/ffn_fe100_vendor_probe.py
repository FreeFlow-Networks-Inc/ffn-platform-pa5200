#!/usr/bin/env python3
"""CP-only compatibility probe using the appliance's own FE100 library.

Set LD_LIBRARY_PATH to the vendor usr/local/lib64 and usr/lib64 directories.
Do not include its old libc directory. Default is library loading only.
The optional attach uses the vendor diagnostic's NONE (33) module selector.
It does not request ASIC initialization or a reset.
"""
import argparse
import ctypes
import os

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--library', default='/opt/ffn-compat/tmp/dpfs/usr/local/lib64/libpandp_cp.so.1.0')
parser.add_argument('--attach', action='store_true')
args = parser.parse_args()
lib = ctypes.CDLL(args.library, mode=os.RTLD_LOCAL | os.RTLD_LAZY)
print('FE100 vendor library loaded', flush=True)
if args.attach:
    init = lib.fe100_init
    init.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    init.restype = ctypes.c_int
    result = init(0, None, 1, 33)
    print('fe100_init(dev=0, slot=1, module=NONE): %d' % result, flush=True)
    raise SystemExit(0 if result == 0 else 1)
