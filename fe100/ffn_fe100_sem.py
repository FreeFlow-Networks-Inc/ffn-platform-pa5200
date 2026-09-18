#!/usr/bin/env python3
"""Initialize SEM after verified FCM DDR; hash-pinned native FE usecase1.

Reference pan_fe100_set_sem_fcm_config_thread calls
fe100_sem_update_configuration before DDR and fe100_sem_fe_init after it.
This separated stage never writes FCM clocks, PHYs, or other lookup blocks.
"""
import argparse
from ffn_fe100_config import load_profile, native_configuration
import ctypes as C
import fcntl
import hashlib
import json
import os
from pathlib import Path
import struct
import time
from ffn_fe100_clocks import LIB,SHA
from ffn_fe100_fcm import ROOT,SNAP,LOOKUP
from ffn_fe100_flow_memory import PROTECTED

REGS=(0x78008,0x78100,0x78104,0x78134,0x7815c,0x78168,0x78174,
      0x78804,0x78808,0x406a4,0x40428,0x40450,*SNAP,*LOOKUP,*PROTECTED)


class Sem:
    def __init__(self,apply=False):
        from ffn_fe100 import bar0_base_and_size,memory_decode_on
        self.lock=open('/run/ffn-fe100-tables.lock','a');fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        if hashlib.sha256(Path(LIB).read_bytes()).hexdigest()!=SHA:raise RuntimeError('owner ABI changed')
        base,size=bar0_base_and_size()
        if size!=0x100000 or not memory_decode_on():raise RuntimeError('FE100 unavailable')
        self.trace=str(ROOT/('sem-io-'+str(time.time_ns())+'.txt'))
        self.shim=C.CDLL('/usr/local/lib/ffn/libffn-fe100-flow-memory.so',mode=os.RTLD_GLOBAL|os.RTLD_NOW)
        if self.shim.ffn_fe100_select_block(0x78000):raise RuntimeError('SEM scope selection failed')
        self.shim.ffn_fe100_open.argtypes=[C.c_uint64,C.c_char_p,C.c_int]
        if self.shim.ffn_fe100_open(base,self.trace.encode(),apply):raise RuntimeError('map failed')
        for r in json.loads(Path('/opt/ffn-compat/opt/ffn/fe100-csr.json').read_text()):
            if 0x78000<=r['addr']<0x80000:self.shim.ffn_fe100_allow(r['addr'])
        for r in REGS:self.shim.ffn_fe100_allow_readonly(r)
        self.shim.fe100_reg_rd.argtypes=[C.c_uint32,C.c_uint32,C.POINTER(C.c_uint32)]
        self.apply=apply

    def read(self,r):
        v=C.c_uint32()
        if self.shim.fe100_reg_rd(0,r,C.byref(v)):raise RuntimeError('read failed '+hex(r))
        return v.value

    def snapshot(self):return {hex(r):self.read(r) for r in REGS}

    def initialize(self):
        if not self.apply:raise RuntimeError('read-only')
        if self.read(0x40428) or self.read(0x40450):raise RuntimeError('sessions must be empty')
        if self.read(0x78804)!=0x100000 or self.read(0x78174) or self.read(0x406a4)&255:
            raise RuntimeError('SEM already initialized; refusing to replay')
        self.lib=C.CDLL(LIB,mode=os.RTLD_LOCAL|os.RTLD_LAZY)
        raw=bytearray(native_configuration(bytes((C.c_char*2812).in_dll(self.lib,'fe100_cfg1')),load_profile()))
        for channel,offset in ((0,1980),(1,2228)):
            r=json.loads((ROOT/('fcm-train-'+str(channel)+'-'+self.boot+'.json')).read_text())
            if r.get('stage')!='completed' or r.get('owner_sha256')!=SHA or r.get('faults')!=0 or r.get('cp_boot_id')!=self.boot:
                raise RuntimeError('successful FCM calibration journal required')
            source=bytes.fromhex(r['configuration_hex'])
            if len(source)!=2812 or struct.unpack_from('>I',source,offset+108)[0]!=channel:
                raise RuntimeError('invalid FCM configuration')
            raw[offset:offset+248]=source[offset:offset+248]
            if self.read(0x98128+channel*32)&0x410!=0x410 or self.read(0x98130+channel*32)&7!=1:
                raise RuntimeError('FCM controller not ready')
        if self.read(0x98008)&3!=3 or self.read(0x98174)&1!=1:raise RuntimeError('FCM clocks not ready')
        struct.pack_into('>I',raw,4,0)
        if any(raw[2804:]):raise RuntimeError('unsupported owner debug mode')
        self.cfg=C.create_string_buffer(bytes(raw),2812)
        protected={r:self.read(r) for r in (*PROTECTED,*LOOKUP,0x98100,0x98174)}
        for name in ('fe100_sem_update_configuration','fe100_sem_fe_init'):
            fn=getattr(self.lib,name);fn.argtypes=[C.c_uint32,C.c_void_p];fn.restype=C.c_int
            self.shim.ffn_flow_watchdog(120)
            try:rc=fn(0,self.cfg)
            finally:self.shim.ffn_flow_watchdog(0)
            if rc or self.shim.ffn_fe100_faults():raise RuntimeError(name+' failed: '+str(rc))
        deadline=time.monotonic()+10
        while time.monotonic()<deadline and not self.read(0x406a4)&255:time.sleep(.05)
        if not self.read(0x406a4)&255:raise RuntimeError('SEM did not supply free counter IDs to FLU')
        if protected!={r:self.read(r) for r in protected}:raise RuntimeError('protected memory state changed')


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--apply',action='store_true');a=p.parse_args()
    io=Sem(a.apply);record={'schema':1,'owner_sha256':SHA,'cp_boot_id':io.boot,'before':io.snapshot(),
                         'trace':io.trace,'session_offload_verified':False}
    if not a.apply:print(json.dumps(record,indent=2));return
    path=ROOT/('sem-init-'+io.boot+'.json')
    with path.open('x') as f:record['stage']='started';json.dump(record,f);f.flush();os.fsync(f.fileno())
    try:io.initialize();record['stage']='completed'
    except BaseException as e:record.update(stage='failed',error=str(e));raise
    finally:
        record.update(after=io.snapshot(),faults=io.shim.ffn_fe100_faults())
        if hasattr(io,'cfg'):record['configuration_hex']=bytes(io.cfg).hex()
        with path.open('w') as f:json.dump(record,f,indent=2);f.flush();os.fsync(f.fileno())
        print(json.dumps({k:v for k,v in record.items() if k!='configuration_hex'},indent=2))


if __name__=='__main__':main()
