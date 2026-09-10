#!/usr/bin/env python3
"""Read the FE100 owner parser table without changing entries or counters."""
import ctypes as C
import fcntl
import hashlib
import json
import os
import struct
from ffn_fe100 import bar0_base_and_size, memory_decode_on
from ffn_fe100_nexthop import LIB, SHA


def open_parser():
    lock = open('/run/ffn-fe100-tables.lock','w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with open(LIB,'rb') as f:
        if hashlib.file_digest(f,'sha256').hexdigest()!=SHA: raise RuntimeError('owner ABI changed')
    base,size=bar0_base_and_size()
    if size!=0x100000 or not memory_decode_on(): raise RuntimeError('FE100 unavailable')
    shim=C.CDLL('/usr/local/lib/ffn/libffn-fe100-tables.so',mode=os.RTLD_GLOBAL|os.RTLD_NOW)
    if shim.ffn_fe100_select_block(0x20000): raise RuntimeError('PAR block selection failed')
    shim.ffn_fe100_open.argtypes=[C.c_uint64,C.c_char_p,C.c_int]
    if shim.ffn_fe100_open(base,b'/var/lib/ffn/fe100/parser-inspect-trace.txt',1): raise RuntimeError('map failed')
    for r in json.load(open('/opt/ffn-compat/opt/ffn/fe100-csr.json')): shim.ffn_fe100_allow(r['addr'])
    lib=C.CDLL(LIB,mode=os.RTLD_LOCAL|os.RTLD_LAZY)
    get=lib.pan_fe100_parser_entry_fetch
    get.argtypes=[C.c_uint32,C.c_void_p,C.c_int]
    get.restype=C.c_int
    put=lib.pan_fe100_parser_entry_insert
    put.argtypes=[C.c_uint32,C.c_void_p,C.c_int]
    put.restype=C.c_int
    return lock,shim,lib,get,put


def main():
    lock,shim,lib,get,put=open_parser()
    result=[]
    for index in range(55):
        entry=(C.c_ubyte*32)()
        rc=get(0,entry,index)
        if rc not in (0,3): raise RuntimeError('parser fetch returned '+str(rc))
        raw=bytes(entry)
        settings,action=struct.unpack_from('>II',raw,24)
        result.append({'index':index,'rc':rc,'raw':raw.hex(),'pt':settings&3,
                       'mode':(settings>>4)&7,'pinit':(settings>>2)&3,
                       'action':(action>>1)&3,'next':(action>>3)&31,'valid':bool(action&1)})
    print(json.dumps(result,indent=2))


if __name__=='__main__': main()
