#!/usr/bin/env python3
"""Read FE100 DDR eye/calibration registers using sysroot PDT coordinates.

IA command/address writes issue PHY reads only. No training, memory patterns,
calibration overrides or status clearing is performed. Eye references are the
sysroot's PA-5260 reference sample; they are comparative diagnostics, not proof
that PA-5220 memory passed training.
"""
import argparse
import ctypes as C
import json
from pathlib import Path
import struct
from ffn_fe100_flow_memory import FlowMemory

# opt/dpfs/usr/share/pdt/fe100.py ddr.eye, FDT0_EYE_REF/FDT1_EYE_REF.
REFERENCES = {
    5: (61,61,62,61,59,60,61,58,62,59,59,57,61,62,61,61,
        62,59,62,61,63,61,63,61,61,62,62,59,62,63,62,61,
        63,63,61,61,62,62,59,61,60,60,60,60,60,60,60,61,
        60,59,62,60,54,57,55,56,58,61,60,63,55,57,63,54),
    6: (60,61,63,62,61,58,56,56,58,56,56,58,60,62,62,60,
        60,58,57,58,49,49,49,49,60,61,60,57,60,59,61,62,
        62,61,61,62,53,62,53,60,63,62,63,59,61,59,60,59,
        62,59,60,62,57,62,59,59,57,61,61,62,62,61,60,60),
    3: (60,61,61,63,61,62,61,62,61,61,61,62,61,60,61,62),
    4: (59,60,60,58,58,58,59,58,60,63,61,63,60,64,62,63),
}


def eye_widths(words):
    return [((word >> shift)&63) or 64 for word in words for shift in (0,8)]


def measurements_present(words, registers):
    # A whole group of zero eye registers accompanied by unset timing values
    # is unmeasured, even though the owner's six-bit conversion yields64.
    return not (not any(words) and not registers['0x19'] and not registers['0x1a'])


def read_spd(io, address):
    handle=C.create_string_buffer(8)
    op=io.lib.libi2c_open; op.argtypes=[C.c_int,C.c_void_p];op.restype=C.c_int
    rd=io.lib.libi2c_read_cmd_byte;rd.argtypes=[C.c_void_p,C.c_uint32,C.c_uint32];rd.restype=C.c_int
    close=io.lib.libi2c_close;close.argtypes=[C.c_void_p];close.restype=C.c_int
    # Same bus/address as sysroot fe100_get_dimm_info DDR4 branch. It restores
    # SPD page0 after its page1 reads; this diagnostic never changes pages.
    io.shim.ffn_flow_watchdog(30)
    values=[]
    opened=False
    try:
        if op(0,handle): raise RuntimeError('SPD bus open failed')
        opened=True
        for offset in range(128):
            value=rd(handle,address,offset)
            if not 0<=value<=255: raise RuntimeError('SPD read failed')
            values.append(value)
    finally:
        try:
            if opened: close(handle)
        finally: io.shim.ffn_flow_watchdog(0)
    return {'address':hex(address),'page0_hex':bytes(values).hex()}


def diagnose(io, channel):
    if channel not in io.channels: raise ValueError('wrong channel for block')
    cfg=C.create_string_buffer(248)
    struct.pack_into('>I',cfg,108,channel)
    fn=io.lib.dphy_reg_rd
    fn.argtypes=[C.c_void_p,C.c_uint32,C.c_uint32,C.c_uint32,C.c_uint32,C.POINTER(C.c_uint32)]
    fn.restype=C.c_int
    def read(block,group,reg):
        value=C.c_uint32()
        io.shim.ffn_flow_watchdog(10)
        try: rc=fn(cfg,block,group,0,reg,C.byref(value))
        finally: io.shim.ffn_flow_watchdog(0)
        if rc or io.shim.ffn_fe100_faults(): raise RuntimeError('DPHY diagnostic read failed')
        return value.value
    calibration={hex(reg):read(3,0,reg) for reg in (0x18,0x19)}
    groups=[]
    for group in range(4 if channel in (5,6) else 1):
        # check_init_cal_status additionally reads DP18 training status17
        # and per-bit errors14. Retain raw values without guessing bit maps.
        regs={hex(r):read(0,group,r) for r in (0x14,0x17,0x18,0x19,0x1a,0x1b)}
        words=[read(0,group,r) for r in range(0x60,0x68)]
        widths=eye_widths(words)
        refs=REFERENCES[channel][group*16:group*16+16]
        present=measurements_present(words,regs)
        groups.append({'group':group,'registers':regs,'eye_raw':words,'eye_widths':widths,
                       'measurements_present':present,
                       'reference_widths':refs,'below_reference_threshold':([group*16+i for i,(w,r) in enumerate(zip(widths,refs)) if w-r<=-12] if present else None)})
    return {'channel':channel,'calibration_registers':calibration,'groups':groups}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--block',choices=('fhm','fdt'),required=True)
    p.add_argument('--spd',action='store_true',help='read FDT DIMM SPD page0 using the sysroot I2C ABI')
    a=p.parse_args()
    io=FlowMemory(a.block,apply=True,diagnostic=True)
    before=io.snapshot()
    channels=[diagnose(io,c) for c in io.channels]
    spd=[read_spd(io,a) for a in (0x53,0x52)] if a.spd and a.block=='fdt' else []
    io.verify_protected()
    after=io.snapshot()
    if before!=after: raise RuntimeError('controller status changed during diagnostics')
    print(json.dumps({'schema':1,'cp_boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'source':'PA-5220 sysroot PDT ddr.eye and owner check_init_cal_status',
        'block':a.block,'channels':channels,'spd':spd,'before':before,'after':after,
        'trace':io.trace,'faults':io.shim.ffn_fe100_faults(),
        'memory_written':False,'training_verified':False},indent=2))


if __name__=='__main__': main()
