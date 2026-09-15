#!/usr/bin/env python3
"""Stage FCM DDR initialization from the PA-5220 sysroot owner sequence.

FCM has a different CSR layout from FHM/FDT. Never reuse their reset offsets.
Default is read-only; clocks and each channel are separately journaled once
per boot. This does not initialize SEM or qualify physical forwarding.
"""
import argparse
import ctypes as C
import json
import os
from pathlib import Path
import struct
import time
from ffn_fe100_flow_memory import FlowMemory, BLOCKS, PROTECTED
from ffn_fe100_clocks import SHA

BLOCKS['fcm']=(0x98000,1896,0,(0,1),0x370)
ROOT=Path('/var/lib/ffn/fe100')
SNAP=(0x98008,0x98100,0x98128,0x98130,0x98148,0x98150,
      0x98160,0x98164,0x98168,0x9816c,0x98170,0x98174,0x98370,0x98374)
LOOKUP=(0xa8100,0xa8104,0xa8134,0xb0100,0xb0104,0xb0134,0x40010)


class Fcm(FlowMemory):
    def __init__(self, apply=False):
        super().__init__('fcm',apply)
        for r in LOOKUP:self.shim.ffn_fe100_allow_readonly(r)
        self.lookup={r:self.read(r) for r in LOOKUP}

    def snapshot(self):
        return {hex(r):self.read(r) for r in SNAP}

    def empty(self):
        if self.read(0x40428) or self.read(0x40450):raise RuntimeError('sessions must be empty')

    def protected_status(self):
        self.verify_protected()
        if self.lookup!={r:self.read(r) for r in LOOKUP}:
            raise RuntimeError('lookup memory state changed')

    def prepare_clocks(self):
        self.empty()
        if self.plan!=[0x07c14100,8,0x2400,0xf8,1]:raise RuntimeError('unexpected FCM PLL plan')
        if self.read(0x98100)!=0x4f3 or self.read(0x98174)&1:
            raise RuntimeError('FCM is not in the observed reset state')
        if [self.read(r) for r in SNAP[6:11]] != [0x07c14101,8,0x2400,0xf8,1]:
            raise RuntimeError('FCM PLL reset values changed')
        for r in (0x98370,0x98374):
            if self.shim.fe100_reg_wr(0,r,(self.read(r)&5)|1) or not self.read(r)&1:
                raise RuntimeError('FCM clock monitor enable failed')
        self.call('fe100_fcm_update_configuration',(C.c_uint32,C.c_void_p),0,self.cfg)
        self.call('fe100_pll_init',(C.c_uint32,C.c_uint32,C.c_void_p),0,0,self.pll)
        for channel in (0,1):self.call('fe100_enable_dram_clks',(C.c_uint32,C.c_uint32),0,channel)
        if self.read(0x98174)&1!=1 or self.read(0x98008)&3!=3 or self.read(0x98100)!=0x400:
            raise RuntimeError('FCM clock/reset verification failed')
        self.protected_status()

    def train(self, channel):
        self.empty()
        if channel not in (0,1):raise ValueError('FCM channel must be 0 or 1')
        if self.read(0x98174)&1!=1 or self.read(0x98008)&3!=3:
            raise RuntimeError('FCM clocks are not ready')
        status=0x98128+32*channel
        if self.read(status)&0x410:raise RuntimeError('refusing to retrain initialized FCM')
        self.call('fe100_dram_initialize_config',(C.c_uint32,C.c_uint32,C.c_void_p),0,channel,self.cfg)
        offset=1980+248*channel
        if struct.unpack_from('>I',self.cfg,offset+108)[0]!=channel:
            raise RuntimeError('FCM configuration channel mismatch')
        self.shim.ffn_flow_verbose(1)
        self.call('fe100_dram_bringup_sequence',(C.c_uint32,C.c_void_p),channel,self.cfg)
        if self.read(status)&0x410!=0x410 or self.read(status+8)&7!=1:
            raise RuntimeError('FCM training/controller readiness failed')
        self.protected_status()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    g=p.add_mutually_exclusive_group();g.add_argument('--clocks',action='store_true');g.add_argument('--train',type=int,choices=(0,1))
    a=p.parse_args();apply=a.clocks or a.train is not None
    io=Fcm(apply);boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    record={'schema':1,'owner_sha256':SHA,'cp_boot_id':boot,'before':io.snapshot(),
            'pll_plan':io.plan,'trace':io.trace,'session_offload_verified':False}
    if not apply:print(json.dumps(record,indent=2));return
    path=ROOT/('fcm-'+('clocks' if a.clocks else 'train-'+str(a.train))+'-'+boot+'.json')
    with path.open('x') as f:
        record['stage']='started';json.dump(record,f);f.flush();os.fsync(f.fileno())
    try:
        if a.clocks:io.prepare_clocks()
        else:io.train(a.train)
        record['stage']='completed'
    except BaseException as e:
        record.update(stage='failed',error=str(e));raise
    finally:
        record.update(after=io.snapshot(),configuration_hex=bytes(io.cfg).hex(),faults=io.shim.ffn_fe100_faults())
        with path.open('w') as f:json.dump(record,f,indent=2);f.flush();os.fsync(f.fileno())
        print(json.dumps({k:v for k,v in record.items() if k!='configuration_hex'},indent=2))


if __name__=='__main__':main()
