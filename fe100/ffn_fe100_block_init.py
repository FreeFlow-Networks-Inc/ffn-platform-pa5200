#!/usr/bin/env python3
"""Lab: initialize an individually audited FE100 packet-processing block."""
import argparse
import ctypes as C
import fcntl
import hashlib
import json
import os
import struct
from ffn_fe100 import bar0_base_and_size, memory_decode_on

p=argparse.ArgumentParser(description=__doc__)
blocks={'tmi':0x8000,'nif':0x10000,'ipq':0x18000,'par':0x20000,'lif':0x28000,'acl':0x30000,
        'dfp':0x38000,'cfp':0x48000,'fwd':0x50000,'lef':0x58000,
        'qmm':0x60000,'lag':0x68000,'prw':0x70000,'tlu':0x80000,'egr':0x88000}
p.add_argument('--block', choices=tuple(blocks), required=True)
p.add_argument('--apply', action='store_true')
p.add_argument('--usecase',type=int,choices=(1,2),default=1,help='1=owner PA-5220 packet broker; 2=FPP diagnostic')
p.add_argument('--parser-json', action='store_true', help='PAR only: load the owner parser overrides in place')
args=p.parse_args()
if args.parser_json and args.block!='par':p.error('--parser-json requires --block par')
lock=open('/run/ffn-fe100-tables.lock','w')
fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
library='/opt/ffn-compat/tmp/dpfs/usr/local/lib64/libpandp_cp.so.1.0'
with open(library,'rb') as f:
    if hashlib.file_digest(f,'sha256').hexdigest()!='b57227a460144c8c2545fc2e268b31f475ef72ac2a6f1d457387ab46d842c3e9':
        raise SystemExit('owner ABI changed')
base,size=bar0_base_and_size()
if size!=0x100000 or not memory_decode_on():raise SystemExit('FE100 BAR unavailable')
shim=C.CDLL('/usr/local/lib/ffn/libffn-fe100-tables.so',mode=os.RTLD_GLOBAL|os.RTLD_NOW)
shim.ffn_fe100_open.argtypes=[C.c_uint64,C.c_char_p,C.c_int]
if shim.ffn_fe100_select_block(blocks[args.block]):raise SystemExit('block selection failed')
trace=('/var/lib/ffn/fe100/'+args.block+'-init.txt').encode()
if shim.ffn_fe100_open(base,trace,args.apply):raise SystemExit('map failed')
for r in json.load(open('/opt/ffn-compat/opt/ffn/fe100-csr.json')):shim.ffn_fe100_allow(r['addr'])
lib=C.CDLL(library,mode=os.RTLD_LOCAL|os.RTLD_LAZY)
# fe100_cfg1 is an exported 2812-byte initialized object in this exact ELF.
# Copy it; never mutate the library's global configuration in place.
owner_defaults=(C.c_char*2812).in_dll(lib,'fe100_cfg1')
cfg=C.create_string_buffer(bytes(owner_defaults),2812)
struct.pack_into('>I',cfg,0,args.usecase)
# Audited IPQ config: check headers/errors; Jericho ITMH; retain pipeline credits.
struct.pack_into('>IIIIII',cfg,1240,1,1,1,0,0,1)
struct.pack_into('>III',cfg,1296,0xffffffff,0xffffffff,255) # documented parser reset controls
# Rewrite lab output toward the MP, using the board's Jericho ITMH format.
struct.pack_into('>I',cfg,1324,8)
# /etc/cfgdb/dp/5200/fe100.cfgdb.xml overrides the generic table partition.
struct.pack_into('>II',cfg,1400,4,2)
struct.pack_into('>IIII',cfg,2476,2,2,1,1)
struct.pack_into('>IIIII',cfg,2576,0,1,1,2,12)
cfg[2596:2608]=bytes((11,10,9,0,8,4,5,6,1,2,7,3))
struct.pack_into('>IIIIIII',cfg,2608,128,256,64,128,256,64,1)
# CSR configuration only: the boot link service already initializes the PHYs.
name=('pan_fe100_'+args.block+'_csr_config' if args.block in ('nif','tmi')
      else 'pan_fe100_set_'+args.block+'_config')
fn=getattr(lib,name)
fn.argtypes=[C.c_uint32,C.c_void_p]
fn.restype=C.c_int
result=fn(0,C.byref(cfg))
if result==0 and args.parser_json:
    source=open('/opt/ffn-compat/tmp/dpfs/etc/fe-parser.json','rb').read()
    json.loads(source) # validate before entering the owner's C parser
    lib.cJSON_Parse.argtypes=[C.c_char_p]
    lib.cJSON_Parse.restype=C.c_void_p
    lib.cJSON_Delete.argtypes=[C.c_void_p]
    lib.cJSON_Delete.restype=None
    root=lib.cJSON_Parse(source)
    if not root:raise SystemExit('owner JSON parse failed')
    try:
        decode=lib.pan_fe100_parser_cjson_decode
        decode.argtypes=[C.c_uint32,C.c_void_p]
        decode.restype=C.c_int
        result=decode(0,root)
        print('Owner parser JSON result=%d'%result,flush=True)
    finally:lib.cJSON_Delete(root)
if args.apply and 'DENIED' in open(trace.decode()).read():
    raise SystemExit('blocked register access occurred; initialization is incomplete')
print('%s init return=%d apply=%s'%(args.block,result,args.apply),flush=True)
raise SystemExit(0 if result==0 else 1)
