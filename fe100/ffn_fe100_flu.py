#!/usr/bin/env python3
"""Commission FLU using the pinned sysroot owner and verified DDR records.

Default is read-only. Apply is exclusive and once per boot, before any live
sessions. DDR, TDI and packet blocks cannot be written through this mapping.
"""
from ffn_fe100 import register_map_path
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
from ffn_fe100_clocks import LIB, SHA
from ffn_fe100_live_sessions import HEALTH, ROOT, calibration_journals, prerequisites
from ffn_fe100_flow_memory import PROTECTED

OFFSETS={3:136,4:384,5:716,6:964}


def completed_targets(trace):
    """Require completion of each cfg4 IA initialization, in submission order."""
    targets=set();pending=None
    for line in trace.splitlines():
        parts=line.split()
        if len(parts)!=3 or parts[0] not in ('R','W'): continue
        op,addr,value=parts[0],int(parts[1],16),int(parts[2],16)
        if op=='W' and addr==0x40800:
            if pending is not None: raise RuntimeError('overlapping initialization commands')
            if (value>>25)&7==4: pending=(value>>1)&0xfffff
        if pending is not None and op=='R' and addr==0x4080c:
            code=(value>>23)&7
            if code==1: targets.add(pending);pending=None
            elif code!=0: raise RuntimeError('IA initialization completion error')
    if pending is not None or targets!=set(1<<i for i in range(11)):
        raise RuntimeError('complete cfg4 initialization trace required')
    return sorted(targets)


def configuration(template, records, boot):
    cfg=bytearray(native_configuration(template,load_profile()))
    if len(cfg)!=2812: raise ValueError('wrong owner configuration size')
    for channel,off in OFFSETS.items():
        r=records.get(channel,{})
        if (r.get('stage')!='completed' or r.get('owner_sha256')!=SHA or
                r.get('cp_boot_id')!=boot or r.get('faults')!=0):
            raise ValueError('verified calibration required for channel '+str(channel))
        raw=bytes.fromhex(r['configuration_hex'])
        if len(raw)!=2812 or struct.unpack_from('>I',raw,off+108)[0]!=channel:
            raise ValueError('incorrect saved channel configuration')
        cfg[off:off+248]=raw[off:off+248]
        if channel==6: cfg[48:52]=raw[48:52] # capacity detected by FDT1 initializer
    struct.pack_into('>I',cfg,4,0)
    if struct.unpack_from('>I',cfg,48)[0]!=4:
        raise ValueError('only the audited FDT capacity4 plan is supported')
    if any(struct.unpack_from('>I',cfg,o+184)[0] for o in OFFSETS.values()):
        raise ValueError('Vref sweep requires a separately audited write scope')
    return bytes(cfg)


class Flu:
    def __init__(self, apply=False):
        from ffn_fe100 import bar0_base_and_size, memory_decode_on
        import sys
        if sys.byteorder!='big' or C.sizeof(C.c_void_p)!=8: raise RuntimeError('CP ABI required')
        self.lock=open('/run/ffn-fe100-tables.lock','a')
        fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        if hashlib.sha256(Path(LIB).read_bytes()).hexdigest()!=SHA: raise RuntimeError('owner ABI changed')
        base,size=bar0_base_and_size()
        if size!=0x100000 or not memory_decode_on(): raise RuntimeError('FE100 BAR unavailable')
        self.trace=str(ROOT/('flu-io-'+str(time.time_ns())+'.txt'))
        self.shim=C.CDLL('/usr/local/lib/ffn/libffn-fe100-flow-memory.so',mode=os.RTLD_GLOBAL|os.RTLD_NOW)
        if self.shim.ffn_fe100_select_block(0x40000): raise RuntimeError('FLU scope unavailable')
        self.shim.ffn_fe100_open.argtypes=[C.c_uint64,C.c_char_p,C.c_int]
        if self.shim.ffn_fe100_open(base,self.trace.encode(),apply): raise RuntimeError('FLU mapping failed')
        for row in json.loads(Path(register_map_path()).read_text()):
            if 0x40000<=row['addr']<0x48000: self.shim.ffn_fe100_allow(row['addr'])
        for r in (*HEALTH,*PROTECTED,0xffffc): self.shim.ffn_fe100_allow_readonly(r)
        self.shim.fe100_reg_rd.argtypes=[C.c_uint32,C.c_uint32,C.POINTER(C.c_uint32)]
        self.apply=apply

    def read(self,r):
        v=C.c_uint32()
        if self.shim.fe100_reg_rd(0,r,C.byref(v)): raise RuntimeError('register read failed '+hex(r))
        return v.value

    def snapshot(self):
        return {hex(r):self.read(r) for r in (*HEALTH,*PROTECTED,0x4080c)}

    def initialize(self):
        if not self.apply: raise RuntimeError('read-only')
        records=calibration_journals(ROOT,self.boot)
        values={r:self.read(r) for r in HEALTH}
        # FLU's own readiness is the work being performed, DDR/CFP must be ready.
        reasons=[s for s in prerequisites(values,self.boot,records) if not s.startswith('FLU ')]
        if reasons: raise RuntimeError('; '.join(reasons))
        if values[0x40428] or values[0x40450] or values[0x40010]:
            raise RuntimeError('requires empty, not-yet-initialized FLU')
        if (self.read(0x4080c)>>23)&7!=1: raise RuntimeError('FLU IA is not idle/complete')
        self.lib=C.CDLL(LIB,mode=os.RTLD_LOCAL|os.RTLD_LAZY)
        raw=configuration(bytes((C.c_char*2812).in_dll(self.lib,'fe100_cfg1')),records,self.boot)
        self.cfg=C.create_string_buffer(raw,2812)
        self.devices=(C.c_ubyte*2176).in_dll(self.lib,'fe100_dev')
        C.c_void_p.from_buffer(self.devices,264).value=C.addressof(self.cfg)
        fn=self.lib.pan_fe100_set_flu_config
        fn.argtypes=[C.c_uint32,C.c_void_p];fn.restype=C.c_int
        self.shim.ffn_flow_verbose(1)
        self.shim.ffn_flow_watchdog(120)
        try: rc=fn(0,self.cfg)
        finally: self.shim.ffn_flow_watchdog(0)
        if rc or self.shim.ffn_fe100_faults(): raise RuntimeError('native FLU initialization failed: '+str(rc))
        self.native_completed=True
        if (self.read(0x4080c)>>23)&7!=1: raise RuntimeError('FLU IA completion failed')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    g=p.add_mutually_exclusive_group();g.add_argument('--apply',action='store_true')
    g.add_argument('--verify-existing',action='store_true',help='read-only verification; never repeats initialization')
    a=p.parse_args()
    io=Flu(a.apply)
    report={'schema':1,'cp_boot_id':io.boot,'owner_sha256':SHA,'before':io.snapshot(),
            'trace':io.trace,'session_offload_verified':False}
    if a.verify_existing:
        prior=json.loads((ROOT/('flu-init-'+io.boot+'.json')).read_text())
        if prior.get('owner_sha256')!=SHA or prior.get('faults')!=0:
            raise RuntimeError('matching fault-free initialization record required')
        if not (prior.get('stage')=='completed' or prior.get('error')=='DDR/TDI status changed during FLU init'):
            raise RuntimeError('initialization outcome requires manual trace audit')
        # Verify each owner's block8 initialization target completed. The
        # trace contains a status read with cc1 after every init command.
        targets=completed_targets(Path(prior['trace']).read_text())
        values={r:io.read(r) for r in HEALTH}
        reasons=prerequisites(values,io.boot,calibration_journals(ROOT,io.boot))
        if reasons: raise RuntimeError('; '.join(reasons))
        report.update(stage='completed',verified_targets=sorted(targets),source_journal=str(ROOT/('flu-init-'+io.boot+'.json')),
                      source_trace_sha256=hashlib.sha256(Path(prior['trace']).read_bytes()).hexdigest(),after=io.snapshot(),faults=io.shim.ffn_fe100_faults())
        for r in (*PROTECTED,0xa8100,0xa8104,0xa8134,0xb0100,0xb0104,0xb0134):
            if report['after'][hex(r)]!=prior['before'][hex(r)]: raise RuntimeError('protected hardware changed')
        path=ROOT/('flu-verified-'+io.boot+'.json')
        with path.open('x') as f: json.dump(report,f,indent=2);f.flush();os.fsync(f.fileno())
        print(json.dumps(report,indent=2));return
    if not a.apply: print(json.dumps(report,indent=2));return
    journal=ROOT/('flu-init-'+io.boot+'.json')
    with journal.open('x') as f:
        report['stage']='started';json.dump(report,f,indent=2);f.flush();os.fsync(f.fileno())
    try:
        io.initialize()
        report['verified_targets']=completed_targets(Path(io.trace).read_text())
        report['after']=io.snapshot()
        # PHY diagnostic bits can change while FLU clears external memory.
        # Check stable clock/reset/TDI values exactly and DDR readiness via
        # the same live masks used by the session endpoint.
        protected=(*PROTECTED,0xa8100,0xa8104,0xa8134,0xb0100,0xb0104,0xb0134)
        if any(report['before'][hex(r)]!=report['after'][hex(r)] for r in protected):
            raise RuntimeError('DDR/TDI status changed during FLU init')
        reasons=prerequisites({r:io.read(r) for r in HEALTH},io.boot,calibration_journals(ROOT,io.boot))
        if reasons: raise RuntimeError('; '.join(reasons))
        report['stage']='completed'
    except BaseException as e:
        report.update(stage='failed',error=str(e));raise
    finally:
        report.update(final_snapshot=io.snapshot(),faults=io.shim.ffn_fe100_faults(),native_completed=getattr(io,'native_completed',False))
        if hasattr(io,'cfg'): report['configuration_hex']=bytes(io.cfg).hex()
        with journal.open('w') as f:
            json.dump(report,f,indent=2);f.flush();os.fsync(f.fileno())
        print(json.dumps(report,indent=2))


if __name__=='__main__': main()
