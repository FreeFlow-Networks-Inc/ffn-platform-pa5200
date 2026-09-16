#!/usr/bin/env python3
"""PA-5220 TDI DDR configuration and clock diagnostics. Default is read-only."""
import argparse
from ffn_fe100_config import load_profile, native_configuration
import ctypes as C
import json
import os
import struct
import time
from ffn_fe100_clocks import Registers,LIB,RST,INIT,TCAM_STATUS,CLOCK_MASK

DDR=(0xa05c0,0xa05c4,0xa05c8,0xa05cc,0xa05d0)
DDR_STATUS=0xa05d4
DDR_MON=(0xa0718,0xa071c)
DPHY_STATUS=0xa01a8
DDR_RESET=(130105601,8,0,0,0)
DDR_PLAN=(130105600,8,0x2400,0xf8,1)
DDR_CLOCK_MASK=(1<<21)|(1<<22)
FIFO_STATUS=(0xa0708,0xa01d4,0xa01e4,0xa01f4,0xa0204,0xa0214,
             0xa0224,0xa0234,0xa0244,0xa0254,0xa0264,0xa0274)


class DDRRegisters(Registers):
    def __init__(self,apply=False,training=False):
        super().__init__(apply, '/usr/local/lib/ffn/libffn-fe100-ddr-mmio.so' if training
                         else '/usr/local/lib/ffn/libffn-fe100-tables.so')
        for register in (*DDR,DDR_STATUS,*DDR_MON,DPHY_STATUS,0xa01b0,0xa0000,*FIFO_STATUS):
            if self.shim.ffn_fe100_allow(register): raise RuntimeError('DDR allowlist failed')
        self.lib=C.CDLL(LIB,mode=os.RTLD_LOCAL|os.RTLD_LAZY)
        if training:
            for register in (0xa00d0,0xa01a0,0xa01a4,0xa01ac,*range(0xa0100,0xa0168,4)):
                if self.shim.ffn_fe100_allow(register): raise RuntimeError('training allowlist failed')
            if self.shim.ffn_fe100_allow_readonly(0xa8080): raise RuntimeError('capability allowlist failed')

    def pll_plan(self):
        fn=self.lib.fe100_pll_initialize_config
        fn.argtypes=[C.c_uint32,C.c_uint32,C.c_void_p]; fn.restype=C.c_int
        cfg=C.create_string_buffer(84)
        if fn(0,1,cfg) or 'DENIED' in open(self.trace).read():
            raise RuntimeError('DDR PLL configuration read failed')
        return list(struct.unpack_from('>5I',cfg,60))

    def snapshot(self):
        return {hex(r):self.read(r) for r in (*DDR,DDR_STATUS,*DDR_MON,DPHY_STATUS,0xa01b0,RST,INIT)}


def prepare_clocks(io,words,sleep=time.sleep):
    if os.path.exists('/var/lib/ffn/fe100/external-tables.json'):
        raise RuntimeError('external-table journal exists; commissioning requires recovery review')
    regs=(*DDR,DDR_STATUS,*DDR_MON,RST,INIT,TCAM_STATUS,0xa0000)
    before={r:io.read(r) for r in regs}
    if tuple(words)!=DDR_PLAN: raise RuntimeError('DDR PLL differs from audited owner plan')
    if tuple(before[r] for r in DDR)!=DDR_RESET or before[RST]!=0x3464:
        raise RuntimeError('expected uninitialized TDI DDR reset state')
    if before[TCAM_STATUS]&1!=1 or before[INIT]&CLOCK_MASK!=CLOCK_MASK:
        raise RuntimeError('verified TCAM clocks required')
    if io.read(0xa0708)&3!=3 or any(io.read(r)&0x1ff for r in FIFO_STATUS[1:]):
        raise RuntimeError('lookup queues are not empty')
    def write(r,value):
        io.write(r,value)
        if io.read(r)!=value: raise RuntimeError('DDR control readback failed at '+hex(r))
    def wait(r,mask):
        for _ in range(500):
            if io.read(r)&mask==mask: return
            sleep(.002)
        raise TimeoutError('DDR clock timeout at '+hex(r))
    try:
        for r in DDR_MON:
            io.write(r,(before[r]&5)|1)
            if not io.read(r)&1: raise RuntimeError('DDR monitor enable failed')
        # Owner pan_fe100_set_tdi_config: system reset, core reset, RCLK, GO.
        reset=before[RST]|0x80; write(RST,reset); sleep(.0025)
        reset|=0x100; write(RST,reset); sleep(.0025)
        reset&=~0x20; write(RST,reset)
        reset|=1; write(RST,reset)
        from ffn_fe100_tcam_sync import synchronize
        synchronize(io,sleep=sleep)
        # cfg4 capability: native owner capture writes low 12 bits = 0x21.
        write(0xa0000,(before[0xa0000]&~0xfff)|0x21)
        write(DDR[0],words[0]|1)
        for r,w in zip(DDR[1:],words[1:]): write(r,w)
        sleep(1)
        write(DDR[0],words[0]&~1); sleep(1)
        wait(DDR_STATUS,1)
        # Exact TDI branch of fe100_enable_dram_clks(type=2).
        for reset in (0x35c1,0x3581,0x0581):
            write(RST,reset); sleep(.001)
        wait(INIT,DDR_CLOCK_MASK)
    except BaseException:
        write(RST,before[RST])
        write(DDR[0],words[0]|1)
        for r in DDR[1:]: write(r,before[r])
        write(DDR[0],before[DDR[0]])
        write(0xa0000,before[0xa0000])
        for r in DDR_MON: io.write(r,before[r]&5)
        raise
    return {'ddr_pll_locked':True,'ddr_clocks_verified':True,
            'training_verified':False,'external_tables_active':False}


def train(io):
    if os.path.exists('/var/lib/ffn/fe100/external-tables.json'):
        raise RuntimeError('external-table journal exists; retraining prohibited')
    if (tuple(io.read(r) for r in DDR)!=DDR_PLAN or io.read(DDR_STATUS)&1!=1 or
        io.read(INIT)&DDR_CLOCK_MASK!=DDR_CLOCK_MASK or io.read(RST)!=0x581):
        raise RuntimeError('DDR clocks/reset preconditions not satisfied')
    cfg=C.create_string_buffer(native_configuration(bytes((C.c_char*2812).in_dll(io.lib,'fe100_cfg1')),load_profile()),2812)
    fn=io.lib.fe100_dram_initialize_config
    fn.argtypes=[C.c_uint32,C.c_uint32,C.c_void_p]; fn.restype=C.c_int
    if fn(0,2,cfg) or io.shim.ffn_fe100_faults():
        raise RuntimeError('DDR configuration failed')
    # Native capture established these complete fallback values after the
    # initializer, rather than the incomplete raw fe100_cfg1 defaults.
    expected=(0,0,0,0,0,1,1,0,0,2,0,0,0,2,0,1,1,0,0,1,1,0,0,0,0,0,0,2,
        1178939696,811549778,1095589716,1145634816,0,0,0,0,0,1,1,0,0,1,0,
        3655,3655,125,0,0,1,1,1,1,1,1,0,1,1,6422632,1,13107202,0,1)
    if struct.unpack_from('>62I',cfg,1576)!=expected:
        raise RuntimeError('board DDR configuration differs from audited runtime fallback')
    journal='/var/lib/ffn/fe100/ddr-training-'+str(time.time_ns())+'.json'
    record={'stage':'training-started','trace':io.trace,'training_verified':False}
    def save():
        with open(journal,'w') as f:
            json.dump(record,f,indent=2); f.flush(); os.fsync(f.fileno())
    save()
    try:
        fn=io.lib.fe100_dram_bringup_sequence
        fn.argtypes=[C.c_uint32,C.c_void_p]; fn.restype=C.c_int
        rc=fn(2,cfg)
        record.update(return_code=rc,after=io.snapshot(),faults=io.shim.ffn_fe100_faults())
        if rc or record['faults']: raise RuntimeError('native DDR bring-up failed: '+str(rc))
        state=io.read(DPHY_STATUS)
        # These are live CSR status bits, not a cached software success flag.
        if state&0x410!=0x410: raise RuntimeError('DPHY init/DP18 PLL status incomplete')
        record.update(stage='controller-trained',training_verified=True)
        save()
        return record
    except BaseException as error:
        record.update(stage='failed',error=str(error),after=io.snapshot())
        save()
        raise


def verify_memory(io,vref=False):
    if os.path.exists('/var/lib/ffn/fe100/external-tables.json'):
        raise RuntimeError('external-table journal exists; destructive DDR tests prohibited')
    if io.read(DPHY_STATUS)&0x410!=0x410 or io.read(RST)&0x4800!=0x4800:
        raise RuntimeError('live DDR training/controller reset status incomplete')
    # Configuration captured from the same hash-pinned native initializer.
    saved=json.load(open('/var/lib/ffn/fe100/ddr-config-audit.json'))
    raw=bytes.fromhex(saved['configuration_hex'])
    if len(raw)!=2812 or struct.unpack_from('>I',raw,1576+108)[0]!=2:
        raise RuntimeError('invalid audited TDI configuration')
    cfg=C.create_string_buffer(raw,len(raw)); dcfg=C.byref(cfg,1576)
    if vref:
        fn=io.lib.fe100_vref_training_sequence
        fn.argtypes=[C.c_uint32,C.c_uint32,C.c_void_p]; fn.restype=C.c_int
        if fn(2,0,cfg) or io.shim.ffn_fe100_faults():
            raise RuntimeError('native Vref training failed')
    # Owner test writes and reads 0xaaaa5555 at TDI DDR diagnostic address0.
    # This commissioning command must precede table activation/session installs.
    if io.shim.ffn_fe100_allow_external_ia():
        raise RuntimeError('external-memory IA bridge unavailable')
    # The owner wrv allocates the block's maximum data width but initializes
    # only four words. Send precisely those four, with no uninitialized tail.
    from ffn_fe100_external_tables import IndirectAccess
    fn=io.lib.fe100_ia_op
    fn.argtypes=[C.c_uint32,C.c_uint32,C.POINTER(IndirectAccess)]; fn.restype=C.c_int
    data=(C.c_uint32*4)(*[0xaaaa5555]*4)
    for operation in (2,1):
        if operation==1:
            for i in range(4): data[i]=0
        req=IndirectAccess(trgt_mem=0x2200,acc_type=operation,addr=0,dcnt=4,data=data)
        rc=fn(0,16,C.byref(req))
        if rc or req.cc!=1 or io.shim.ffn_fe100_faults():
            raise RuntimeError('DDR write/read verification failed: '+str(rc))
    if data[0]&0xffff!=0x5555 or list(data)[1:]!=[0xaaaa5555]*3:
        raise RuntimeError('DDR pattern mismatch: '+str(list(data)))
    fn=io.lib.dphy_reg_rd
    fn.argtypes=[C.c_void_p,C.c_uint32,C.c_uint32,C.c_uint32,C.c_uint32,C.POINTER(C.c_uint32)]
    fn.restype=C.c_int
    widths=[]
    for pair in range(8):
        value=C.c_uint32()
        if fn(dcfg,0,0,0,0x60+pair,C.byref(value)):
            raise RuntimeError('DDR eye read failed')
        widths.extend(((value.value>>shift)&63) or 64 for shift in (0,8))
    refs=(61,62,61,62,61,61,59,60,62,63,60,61,61,60,61,62)
    # Sysroot PDT criterion: fail if reference difference <= -12.
    passed=all(w-r>-12 for w,r in zip(widths,refs))
    result={'ddr_memory_test_verified':True,'eye_widths':widths,'eye_reference':refs,
            'eye_test_passed':passed,'vref_sequence_completed':vref,
        'after':io.snapshot(),'trace':io.trace,'external_tables_active':False,
        'tested_at_ns':time.time_ns(),
        'cp_boot_id':open('/proc/sys/kernel/random/boot_id').read().strip()}
    if io.shim.ffn_fe100_faults() or not passed:
        raise RuntimeError('DDR eye verification failed: '+json.dumps(result))
    with open('/var/lib/ffn/fe100/ddr-memory-verified.json','w') as f:
        json.dump(result,f,indent=2); f.flush(); os.fsync(f.fileno())
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--prepare-clocks',action='store_true')
    p.add_argument('--train',action='store_true')
    p.add_argument('--verify-memory',action='store_true')
    p.add_argument('--vref',action='store_true')
    args=p.parse_args()
    if sum((args.prepare_clocks,args.train,args.verify_memory))>1: p.error('run stages separately')
    if args.vref and not args.verify_memory: p.error('--vref requires --verify-memory')
    io=DDRRegisters(args.prepare_clocks or args.train or args.verify_memory,
                    training=args.train or args.verify_memory)
    plan=io.pll_plan()
    print(json.dumps({'before':io.snapshot(),'pll_plan':plan,
        'trace':io.trace,'training_verified':False},indent=2))
    if args.prepare_clocks: print(json.dumps(prepare_clocks(io,plan)))
    if args.train: print(json.dumps(train(io),indent=2))
    if args.verify_memory: print(json.dumps(verify_memory(io,args.vref),indent=2))


if __name__=='__main__': main()
