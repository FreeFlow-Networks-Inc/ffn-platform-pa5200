#!/usr/bin/env python3
"""Commission FE100 FHM/FDT memory, separately from TDI external lookup RAM.

Default reads configuration/status only. Native routines are bound to the
appliance's audited ELF and one writable register block. Each training attempt
is journaled before hardware I/O; an interrupted attempt is never retried.
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
from ffn_fe100_clocks import LIB, SHA

BLOCKS = {'fhm': (0xa8000, 52, 4, (3, 4), 0x370),
          'fdt': (0xb0000, 632, 3, (5, 6), 0x610)}
PROTECTED = (0xa0004, 0xa01c0, 0xa01a8, 0xa01b0)


def enable_fdt1_init_pattern(cfg):
    """Supply the missing INIT_PAT_WRITE flag for the observed DDR4 profile.

    Pinned owner: FDT0 initializer stores1 at cfg716+188 (0x102ddaa8).
    FDT1 initializer leaves cfg964+188 at its static zero. The sequencer at
    0x102e604c skips INIT_PAT_WRITE when zero, before READ_CENTERING.
    Limit this correction to the observed type/width/DIMM profile and retain
    every calibration/error check. No lane masks or timing overrides.
    """
    if len(cfg) != 2812: raise ValueError('unexpected FE100 configuration size')
    off=964
    expected={20:1,24:1,28:1,108:6,148:1,152:1,164:1}
    if any(struct.unpack_from('>I',cfg,off+k)[0]!=v for k,v in expected.items()):
        raise ValueError('FDT1 pattern correction requires the audited DDR4 profile')
    prior=struct.unpack_from('>I',cfg,off+188)[0]
    if prior not in (0,1): raise ValueError('invalid INIT_PAT_WRITE flag')
    struct.pack_into('>I',cfg,off+188,1)
    return {'field_offset':off+188,'previous':prior,'value':1,'algorithm':'INIT_PAT_WRITE'}


def pll_override(raw):
    """Owner set_fhm_config(freq=0)/set_fdt_config: audited 800 MHz plan."""
    words = list(struct.unpack_from('>5I', raw, 60))
    for shift, width, value in ((8, 6, 1), (14, 2, 1), (16, 5, 1),
                                 (21, 5, 30), (26, 2, 1)):
        mask = ((1 << width)-1) << shift
        words[0] = (words[0] & ~mask) | (value << shift)
    words[1] = (words[1] & ~255) | 8
    struct.pack_into('>5I', raw, 60, *words)
    return words


class FlowMemory:
    def __init__(self, block, apply=False, diagnostic=False, lock_fd=None):
        from ffn_fe100 import bar0_base_and_size, memory_decode_on
        self.name = block
        self.base, self.offset, self.pll_type, self.channels, self.monitor = BLOCKS[block]
        if lock_fd is not None:
            if os.readlink('/proc/self/fd/'+str(lock_fd))!='/run/ffn-fe100-tables.lock':
                raise RuntimeError('invalid inherited table lock')
            self.lock=os.fdopen(os.dup(lock_fd),'a')
        else:self.lock = open('/run/ffn-fe100-tables.lock', 'a')
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if hashlib.sha256(Path(LIB).read_bytes()).hexdigest() != SHA:
            raise RuntimeError('owner ABI changed')
        base, size = bar0_base_and_size()
        if size != 0x100000 or not memory_decode_on():
            raise RuntimeError('FE100 BAR unavailable')
        self.trace = '/var/lib/ffn/fe100/'+block+'-memory-'+str(time.time_ns())+'.txt'
        self.shim = C.CDLL('/usr/local/lib/ffn/'+('libffn-fe100-diagnostic.so' if diagnostic else 'libffn-fe100-flow-memory.so'),
                           mode=os.RTLD_GLOBAL | os.RTLD_NOW)
        self.shim.ffn_fe100_open.argtypes = [C.c_uint64, C.c_char_p, C.c_int]
        if self.shim.ffn_fe100_select_block(self.base):
            raise RuntimeError('flow memory block selection failed')
        if diagnostic and self.shim.ffn_flow_diagnostic_mode():
            raise RuntimeError('flow memory diagnostic scope failed')
        if self.shim.ffn_fe100_open(base, self.trace.encode(), apply):
            raise RuntimeError('flow memory mapping failed')
        for r in json.loads(Path('/opt/ffn-compat/opt/ffn/fe100-csr.json').read_text()):
            if self.base <= r['addr'] < self.base+0x8000:
                self.shim.ffn_fe100_allow(r['addr'])
        for r in (*PROTECTED, 0xa8080, 0xb0080, 0x40404, 0x40428, 0x40450, 0xffffc):
            self.shim.ffn_fe100_allow_readonly(r)
        self.shim.fe100_reg_rd.argtypes = [C.c_uint32, C.c_uint32, C.POINTER(C.c_uint32)]
        self.shim.fe100_reg_wr.argtypes = [C.c_uint32, C.c_uint32, C.c_uint32]
        self.lib = C.CDLL(LIB, mode=os.RTLD_LOCAL | os.RTLD_LAZY)
        self.cfg = C.create_string_buffer(native_configuration(bytes((C.c_char*2812).in_dll(self.lib, 'fe100_cfg1')),load_profile()),2812)
        struct.pack_into('>I', self.cfg, 4, 0)
        self.pll = C.create_string_buffer(84)
        self.call('fe100_pll_initialize_config', (C.c_uint32,C.c_uint32,C.c_void_p), 0, self.pll_type, self.pll)
        self.plan = pll_override(self.pll)
        self.protected = {r: self.read(r) for r in PROTECTED}

    def read(self, r):
        value = C.c_uint32()
        if self.shim.fe100_reg_rd(0, r, C.byref(value)):
            raise RuntimeError('register read failed: '+hex(r))
        return value.value

    def write(self, r, value):
        if self.shim.fe100_reg_wr(0, r, value) or self.read(r) != value:
            raise RuntimeError('register write/readback failed: '+hex(r))

    def call(self, name, types, *args):
        fn = getattr(self.lib, name)
        fn.argtypes, fn.restype = list(types), C.c_int
        self.shim.ffn_flow_watchdog(120)
        try:
            rc = fn(*args)
        finally:
            self.shim.ffn_flow_watchdog(0)
        if rc or self.shim.ffn_fe100_faults():
            raise RuntimeError(name+' failed: '+str(rc))

    def snapshot(self):
        return {hex(self.base+r): self.read(self.base+r) for r in
                (0x100,0x104,0x120,0x124,0x128,0x12c,0x130,0x134,
                 0x148,0x150,0x168,0x170)}

    def verify_protected(self):
        if {r: self.read(r) for r in PROTECTED} != self.protected:
            raise RuntimeError('external lookup status changed during commissioning')

    def prepare_clocks(self):
        b = self.base
        if self.plan != [0x07c14100,8,0x2400,0xf8,1]:
            raise RuntimeError('PLL plan differs from the audited board configuration')
        if self.read(b+0x104) != 0xf3 or self.read(b+0x134)&1:
            raise RuntimeError('flow memory is not in reset; refusing to retune')
        if self.read(0x40428) or self.read(0x40450):
            raise RuntimeError('hardware contains sessions')
        for off in (self.monitor, self.monitor+4):
            if self.shim.fe100_reg_wr(0, b+off, (self.read(b+off)&5)|1) or not self.read(b+off)&1:
                raise RuntimeError('flow memory clock monitor did not enable')
        self.call('fe100_pll_init', (C.c_uint32,C.c_uint32,C.c_void_p), 0, self.pll_type, self.pll)
        if not self.read(b+0x134)&1:
            raise RuntimeError('flow memory PLL did not lock')
        for channel in self.channels:
            self.call('fe100_enable_dram_clks', (C.c_uint32,C.c_uint32), 0, channel)
        if self.read(b+0x104) != 0 or self.read(b+0x100)&3 != 3:
            raise RuntimeError('flow memory clock/reset readback incomplete')
        self.verify_protected()

    def train(self, channel):
        if channel not in self.channels:
            raise ValueError('wrong memory channel')
        b = self.base
        if not self.read(b+0x134)&1 or self.read(b+0x100)&3 != 3:
            raise RuntimeError('verified flow memory clocks required')
        if [self.read(b+r) for r in (0x120,0x124,0x128,0x12c,0x130)] != [0x07c14100,8,0x2400,0xf8,1]:
            raise RuntimeError('flow memory PLL configuration changed')
        slot = self.channels.index(channel)
        if self.read(b+0x148+slot*32)&0x410:
            raise RuntimeError('channel already trained; refusing retraining')
        if self.read(0x40428) or self.read(0x40450):
            raise RuntimeError('hardware contains sessions')
        self.call('fe100_dram_initialize_config', (C.c_uint32,C.c_uint32,C.c_void_p), 0, channel, self.cfg)
        if channel == 6: self.correction = enable_fdt1_init_pattern(self.cfg)
        self.call('fe100_dram_bringup_sequence', (C.c_uint32,C.c_void_p), channel, self.cfg)
        if self.read(b+0x148+slot*32)&0x410 != 0x410:
            raise RuntimeError('flow DDR DPHY training incomplete')
        self.verify_protected()

    def recover_calibration(self, boot):
        # One bounded FDT1 post-initialization retry, no shared PLL/reset writes.
        # The original failed journal remains intact. DPHY init-done alone did
        # not expose calibration error 0x808, so never use it to adopt a bank.
        if self.name != 'fdt':
            raise RuntimeError('only the observed FDT1 calibration recovery is supported')
        previous = json.loads(Path('/var/lib/ffn/fe100/fdt-train-6-'+boot+'.json').read_text())
        if previous.get('stage') != 'failed' or previous.get('error') != 'fe100_dram_bringup_sequence failed: -1':
            raise RuntimeError('no matching failed calibration journal')
        if self.read(0x40428) or self.read(0x40450):
            raise RuntimeError('hardware contains sessions')
        if self.read(self.base+0x104) != 0xf0c or self.read(self.base+0x134) != 1:
            raise RuntimeError('FDT1 recovery reset/clock state changed')
        sibling = (self.read(self.base+0x148), self.read(self.base+0x150))
        self.call('fe100_dram_initialize_config', (C.c_uint32,C.c_uint32,C.c_void_p), 0, 6, self.cfg)
        self.call('fe100_dram_post_init', (C.c_void_p,), C.byref(self.cfg,964))
        if (self.read(self.base+0x148), self.read(self.base+0x150)) != sibling:
            raise RuntimeError('sibling memory status changed')
        self.verify_protected()

    def recover_controller(self, boot, init_pattern=False):
        """Reset only failed FDT1 before repeating the owner's full sequence.

        Sysroot FDT_RST_CTRL: channel1 UMCTL reset_l bit3, PHY reset/recover
        bits5/7, powerup/init markers bits9/11. Preserve channel0 and shared
        clock bits. The earlier post-init-only recovery retained partial PHY
        calibration; this stage begins from an isolated controller reset.
        """
        if self.name != 'fdt': raise RuntimeError('FDT1 recovery only')
        prior_stage='recover-controller' if init_pattern else 'recover-calibration'
        previous=json.loads(Path('/var/lib/ffn/fe100/fdt-'+prior_stage+'-6-'+boot+'.json').read_text())
        expected_error=('fe100_dram_bringup_sequence' if init_pattern else 'fe100_dram_post_init')+' failed: -1'
        if (previous.get('stage')!='failed' or previous.get('error')!=expected_error or
                previous.get('cp_boot_id')!=boot or previous.get('owner_sha256')!=SHA or previous.get('faults')!=0):
            raise RuntimeError('matching failed recovery required')
        if self.read(0x40428) or self.read(0x40450) or self.read(0x40404)&0x1e000:
            raise RuntimeError('flow tables must be empty with FLU memory interfaces in reset')
        if self.read(0xb0104)!=0xf0c or self.read(0xb0134)!=1:
            raise RuntimeError('unexpected recovery clock/reset state')
        sibling=(self.read(0xb0148),self.read(0xb0150))
        self.call('fe100_dram_initialize_config',(C.c_uint32,C.c_uint32,C.c_void_p),0,6,self.cfg)
        if init_pattern: self.correction = enable_fdt1_init_pattern(self.cfg)
        self.shim.ffn_flow_verbose(1)
        self.write(0xb0104,0x5a4)
        time.sleep(.001)
        # Owner fe100_enable_dram_clks(type6) releases bits5/7 by RMW.
        self.call('fe100_enable_dram_clks',(C.c_uint32,C.c_uint32),0,6)
        if self.read(0xb0104)!=0x504: raise RuntimeError('isolated PHY reset release failed')
        try:
            self.call('fe100_dram_bringup_sequence',(C.c_uint32,C.c_void_p),6,self.cfg)
        finally:
            if (self.read(0xb0148),self.read(0xb0150))!=sibling:
                raise RuntimeError('FDT0 status changed during FDT1 recovery')
            self.verify_protected()
        if self.read(0xb0168)&0x410!=0x410 or self.read(0xb0170)&7!=1:
            raise RuntimeError('FDT1 controller readiness incomplete')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--block', required=True, choices=BLOCKS)
    g = p.add_mutually_exclusive_group()
    g.add_argument('--prepare-clocks', action='store_true')
    g.add_argument('--train', type=int, choices=(3,4,5,6))
    g.add_argument('--recover-calibration', action='store_true')
    g.add_argument('--recover-controller', action='store_true')
    g.add_argument('--recover-init-pattern', action='store_true')
    a = p.parse_args()
    apply = a.prepare_clocks or a.train is not None or a.recover_calibration or a.recover_controller or a.recover_init_pattern
    io = FlowMemory(a.block, apply)
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    report = {'schema':1,'cp_boot_id':boot,'block':a.block,'owner_sha256':SHA,
              'before':io.snapshot(),'pll_plan':io.plan,'trace':io.trace,
              'session_offload_verified':False}
    if apply:
        stage = 'recover-init-pattern-6' if a.recover_init_pattern else ('recover-controller-6' if a.recover_controller else ('recover-calibration-6' if a.recover_calibration else ('clocks' if a.prepare_clocks else 'train-'+str(a.train))))
        journal = Path('/var/lib/ffn/fe100/'+a.block+'-'+stage+'-'+boot+'.json')
        # O_EXCL prevents retries after crash/timeout/partially trained memory.
        with journal.open('x') as f:
            report['stage'] = 'started'
            json.dump(report, f, indent=2); f.flush(); os.fsync(f.fileno())
        try:
            if a.prepare_clocks: io.prepare_clocks()
            elif a.recover_calibration: io.recover_calibration(boot)
            elif a.recover_controller: io.recover_controller(boot)
            elif a.recover_init_pattern: io.recover_controller(boot, init_pattern=True)
            else: io.train(a.train)
            report['stage'] = 'completed'
        except BaseException as e:
            report.update(stage='failed', error=str(e))
            raise
        finally:
            report.update(after=io.snapshot(),faults=io.shim.ffn_fe100_faults())
            if a.train is not None or a.recover_calibration or a.recover_controller or a.recover_init_pattern: report['configuration_hex'] = bytes(io.cfg).hex()
            if hasattr(io,'correction'): report['correction'] = io.correction
            with journal.open('w') as f:
                json.dump(report, f, indent=2); f.flush(); os.fsync(f.fileno())
            print(json.dumps(report,indent=2))
    else:
        print(json.dumps(report,indent=2))


if __name__ == '__main__': main()
