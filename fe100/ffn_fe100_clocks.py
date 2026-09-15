#!/usr/bin/env python3
"""PA-5220 external lookup clock bring-up, stage 1: TCAM PLL and clock resets.

Default is a read-only plan. DDR PHY/training and external TCAM table setup
are separate stages; clock lock alone never qualifies session offload.
"""
import argparse
import ctypes as C
import fcntl
import hashlib
import json
import os
import struct
import time

LIB = '/opt/ffn-compat/tmp/dpfs/usr/local/lib64/libpandp_cp.so.1.0'
SHA = 'b57227a460144c8c2545fc2e268b31f475ef72ac2a6f1d457387ab46d842c3e9'
RST, INIT = 0xa0004, 0xa01c0
TCAM = (0xa05a0, 0xa05a4, 0xa05a8, 0xa05ac, 0xa05b0)
TCAM_STATUS = 0xa05b4
MONITORS = (0xa0710, 0xa0714)
CLOCK_MASK = (1 << 24) | (1 << 25)
# Exact PA-5220 reset-state plan from the hash-pinned owner's read-only config
# helper plus its TDI overrides, verified against the live register defaults.
TCAM_RESET = (0x0e9d4201, 15, 0, 0, 0)
TCAM_PLAN = (0x07bd4120, 10, 0x2400, 0xf8, 1)


def tcam_config(config):
    """Apply only the TCAM overrides in owner pan_fe100_set_tdi_config.

    DWARF: 84-byte PLL config, ctrl1 at60, ctrl2 at64. Owner instructions
    0x102b7e20..0x102b7e60 set these fields before fe100_pll_init(type=2).
    """
    if len(config) != 84:
        raise ValueError('unexpected PLL configuration ABI')
    words = list(struct.unpack_from('>5I', config, 60))
    for shift, width, value in ((8,6,1), (14,2,1), (16,5,29),
                                 (21,5,29), (26,2,1), (5,1,1)):
        mask = ((1 << width)-1) << shift
        words[0] = (words[0] & ~mask) | (value << shift)
    words[1] = (words[1] & ~255) | 10
    return words


def apply_tcam(io, words, sleep=time.sleep, clock=time.monotonic):
    if len(words) != 5 or any(type(w) is not int or not 0 <= w <= 0xffffffff for w in words):
        raise ValueError('invalid PLL words')
    before = {r: io.read(r) for r in (*TCAM, RST, INIT, TCAM_STATUS, *MONITORS)}
    if (tuple(words) == TCAM_PLAN and tuple(before[r] for r in TCAM) == TCAM_PLAN
            and before[INIT] & CLOCK_MASK == CLOCK_MASK and before[TCAM_STATUS] & 1
            and not before[RST] & 0x1b and all(before[r] & 1 for r in MONITORS)):
        return {'changed':False, 'tcam_pll_locked':True, 'init_status':before[INIT],
                'external_memory_initialized':False, 'session_offload_verified':False}
    # Only commission the reset/off state. Never retune a running PLL or table.
    if (before[INIT] & CLOCK_MASK or before[TCAM_STATUS] & 1 or
            not before[TCAM[0]] & 1 or before[RST] & 1 or
            before[RST] & 0x1a != 0x1a):
        raise RuntimeError('TCAM PLL/clock domains are not in the reset/off state')
    # Owner initialize_config also supplies analog loop tuning. Accept only
    # the fully audited reset-state plan, never caller-chosen frequencies.
    if tuple(before[r] for r in TCAM) != TCAM_RESET or tuple(words) != TCAM_PLAN:
        raise RuntimeError('PLL configuration differs from audited PA-5220 reset-state plan')
    def write(register, value):
        io.write(register, value)
        if io.read(register) != value:
            raise RuntimeError('clock register readback failed at '+hex(register))
    def wait_for(register, mask):
        deadline = clock() + 1.0
        for _ in range(501):
            if io.read(register) & mask == mask:
                return
            if clock() >= deadline:
                break
            sleep(.002)
        raise TimeoutError('clock lock/status timeout at '+hex(register))
    try:
        for monitor in MONITORS:
            # Register-map EN bit activates clock energy monitoring. Status
            # and frequency fields are read-only; write only control bits.
            io.write(monitor, (before[monitor] & 5) | 1)
            if not io.read(monitor) & 1:
                raise RuntimeError('clock monitor enable did not read back')
        write(TCAM[0], words[0] | 1)
        for register, value in zip(TCAM[1:], words[1:]):
            write(register, value)
        sleep(1)  # owner PLL reset assert and deassert delays, seconds
        write(TCAM[0], words[0] & ~1)
        sleep(1)
        wait_for(TCAM_STATUS, 1)
        # Owner TDI sequence releases TWG first, then 1x/2x soft resets.
        write(RST, before[RST] & ~2)
        write(RST, before[RST] & ~0x1a)
        wait_for(INIT, CLOCK_MASK)
    except BaseException:
        # No memory training/table writes occurred. Return clock domains and
        # PLL to their initial reset state before restoring divider settings.
        write(RST, before[RST])
        write(TCAM[0], words[0] | 1)
        for register in TCAM[1:]:
            write(register, before[register])
        write(TCAM[0], before[TCAM[0]])
        for monitor in MONITORS:
            io.write(monitor, before[monitor] & 5)
            if io.read(monitor) & 5 != before[monitor] & 5:
                raise RuntimeError('clock monitor rollback failed')
        raise
    return {'changed':True, 'tcam_pll_locked': bool(io.read(TCAM_STATUS) & 1),
            'init_status': io.read(INIT), 'external_memory_initialized': False,
            'session_offload_verified': False}


class Registers:
    def __init__(self, apply, shim_path='/usr/local/lib/ffn/libffn-fe100-tables.so'):
        from ffn_fe100 import bar0_base_and_size, memory_decode_on
        self.lock = open('/run/ffn-fe100-tables.lock', 'a')
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with open(LIB, 'rb') as source:
            if hashlib.file_digest(source, 'sha256').hexdigest() != SHA:
                raise RuntimeError('owner PLL ABI changed')
        base, size = bar0_base_and_size()
        if size != 0x100000 or not memory_decode_on():
            raise RuntimeError('FE100 BAR unavailable')
        self.trace = '/var/lib/ffn/fe100/clock-init-'+str(time.time_ns())+'.txt'
        self.shim = C.CDLL(shim_path,
                           mode=os.RTLD_GLOBAL | os.RTLD_NOW)
        self.shim.ffn_fe100_open.argtypes = [C.c_uint64, C.c_char_p, C.c_int]
        if self.shim.ffn_fe100_select_block(0xa0000):
            raise RuntimeError('TDI block selection failed')
        if self.shim.ffn_fe100_open(base, self.trace.encode(), int(apply)):
            raise RuntimeError('TDI mapping failed')
        for register in (*TCAM, TCAM_STATUS, RST, INIT, *MONITORS):
            if self.shim.ffn_fe100_allow(register):
                raise RuntimeError('register allowlist failed')
        self.shim.fe100_reg_rd.argtypes = [C.c_uint32,C.c_uint32,C.POINTER(C.c_uint32)]
        self.shim.fe100_reg_wr.argtypes = [C.c_uint32,C.c_uint32,C.c_uint32]

    def read(self, register):
        value = C.c_uint32()
        if self.shim.fe100_reg_rd(0, register, C.byref(value)):
            raise RuntimeError('clock register read failed')
        return value.value

    def write(self, register, value):
        if self.shim.fe100_reg_wr(0, register, value):
            raise RuntimeError('clock register write failed')

    def plan(self):
        library = C.CDLL(LIB, mode=os.RTLD_LOCAL | os.RTLD_LAZY)
        fn = library.fe100_pll_initialize_config  # reads CSRs; does not program PLL
        fn.argtypes = [C.c_uint32, C.c_uint32, C.c_void_p]
        fn.restype = C.c_int
        cfg = C.create_string_buffer(84)
        if fn(0, 2, cfg) or 'DENIED' in open(self.trace).read():
            raise RuntimeError('owner TCAM PLL configuration read failed')
        return tcam_config(bytes(cfg))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply-tcam', action='store_true')
    args = p.parse_args()
    io = Registers(args.apply_tcam)
    words = io.plan()
    report = {'stage':'tcam-pll', 'apply':args.apply_tcam,
              'trace':io.trace,
              'registers': {hex(r):hex(w) for r,w in zip(TCAM,words)},
              'before': {hex(r):hex(io.read(r)) for r in (*TCAM, RST, INIT, TCAM_STATUS, *MONITORS)}}
    print(json.dumps(report), flush=True)
    if args.apply_tcam:
        print(json.dumps(apply_tcam(io, words)), flush=True)


if __name__ == '__main__': main()
