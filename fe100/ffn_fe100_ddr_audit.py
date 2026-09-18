#!/usr/bin/env python3
"""Run owner DDR configuration against an offline register model, not hardware."""
import argparse
from ffn_fe100_config import load_profile, native_configuration
import ctypes as C
import hashlib
import json
import os
import struct
from ffn_fe100_clocks import LIB,SHA


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('capture_library')
    p.add_argument('--output',required=True)
    p.add_argument('--initialize-model',action='store_true')
    args=p.parse_args()
    with open(LIB,'rb') as f:
        if hashlib.file_digest(f,'sha256').hexdigest()!=SHA:
            raise RuntimeError('owner ABI changed')
    shim=C.CDLL(args.capture_library,mode=os.RTLD_GLOBAL|os.RTLD_NOW)
    lib=C.CDLL(LIB,mode=os.RTLD_LOCAL|os.RTLD_LAZY)
    cfg=C.create_string_buffer(native_configuration(bytes((C.c_char*2812).in_dll(lib,'fe100_cfg1')),load_profile()),2812)
    capability=lib.pan_fe100_tdi_set_capability
    capability.argtypes=[C.c_uint32,C.c_uint32]; capability.restype=C.c_int
    if capability(0,4): raise RuntimeError('capability capture failed')
    fn=lib.fe100_dram_initialize_config
    fn.argtypes=[C.c_uint32,C.c_uint32,C.c_void_p]; fn.restype=C.c_int
    rc=fn(0,2,cfg)
    if not rc and args.initialize_model:
        init=lib.fe100_dram_init
        init.argtypes=[C.c_void_p]; init.restype=C.c_int
        rc=init(C.byref(cfg,1576))
    report={'hardware_access':False,'training_verified':False,'return_code':rc,
            'denied':shim.ffn_ddr_capture_denied(),'configuration_hex':bytes(cfg).hex(),
            'tdi_ddr_words':list(struct.unpack_from('>62I',cfg,1576))}
    with open(args.output,'w') as f: json.dump(report,f,indent=2)
    print(json.dumps({k:v for k,v in report.items() if k!='configuration_hex'}),flush=True)
    if rc or report['denied']: raise SystemExit(1)


if __name__=='__main__': main()
