#!/usr/bin/env python3
"""Commission front1 LIF3 -> DIRECT next-hop31 for 20 seconds, then restore.

Invoked only by ffn_fe100_nexthop.py while it owns the table lock and the
temporary next-hop. This child has a separate MMIO mapping restricted to TLU.
"""
import ctypes as C
import hashlib
import json
import os
import signal
import struct
import time
from ffn_fe100 import Fe100, bar0_base_and_size, memory_decode_on


def main():
    def interrupted(signum, frame):
        raise SystemExit(128+signum)
    signal.signal(signal.SIGTERM,interrupted)
    signal.signal(signal.SIGINT,interrupted)
    # An inherited locked FD proves this child is inside the commissioning
    # transaction; do not expose an unlocked standalone table writer.
    fd = int(os.environ['FFN_FE100_LOCK_FD'])
    if os.readlink('/proc/self/fd/'+str(fd)) != '/run/ffn-fe100-tables.lock':
        raise RuntimeError('missing parent table lock')
    libpath = '/opt/ffn-compat/tmp/dpfs/usr/local/lib64/libpandp_cp.so.1.0'
    with open(libpath, 'rb') as f:
        if hashlib.file_digest(f, 'sha256').hexdigest() != 'b57227a460144c8c2545fc2e268b31f475ef72ac2a6f1d457387ab46d842c3e9':
            raise RuntimeError('owner ABI changed')
    base, size = bar0_base_and_size()
    if size != 0x100000 or not memory_decode_on():
        raise RuntimeError('FE100 unavailable')
    shim = C.CDLL('/usr/local/lib/ffn/libffn-fe100-tables.so', mode=os.RTLD_GLOBAL | os.RTLD_NOW)
    if shim.ffn_fe100_select_block(0x80000) or shim.ffn_fe100_select_lif_table(0):
        raise RuntimeError('table selection failed')
    shim.ffn_fe100_open.argtypes = [C.c_uint64, C.c_char_p, C.c_int]
    if shim.ffn_fe100_open(base, b'/var/lib/ffn/fe100/direct-lif-trace.txt', 1):
        raise RuntimeError('map failed')
    for r in json.load(open('/opt/ffn-compat/opt/ffn/fe100-csr.json')):
        shim.ffn_fe100_allow(r['addr'])
    lib = C.CDLL(libpath, mode=os.RTLD_LOCAL | os.RTLD_LAZY)
    get, put = lib.pan_fe100_fetch_lif_entry, lib.pan_fe100_insert_lif_entry
    for fn in (get, put):
        fn.argtypes = [C.c_uint32, C.c_void_p, C.c_uint32]
        fn.restype = C.c_int
    original = (C.c_ubyte * 36)()
    if get(0, original, 3):
        raise RuntimeError('cannot read lab LIF3')
    before = bytes(original)
    if (struct.unpack_from('>III', before, 4) != (0x80050000, 8, 1 << 16)
            or (int.from_bytes(before[16:26], 'big') >> 32) & 63 != 63
            or (int.from_bytes(before[26:36], 'big') >> 32) & 63 != 1):
        raise RuntimeError('LIF3 differs from verified front1 baseline')
    wanted = bytearray(before)
    capture = os.environ.get('FFN_FE100_CAPTURE') == '1'
    struct.pack_into('>II', wanted, 4, 0x80040000 | ((1 << 28) if capture else 0), 31)
    entry = (C.c_ubyte * 36).from_buffer_copy(wanted)
    fe = Fe100() if capture else None
    modes = {}
    try:
        if capture:
            registers = {r['name']:r['addr'] for r in json.load(open('/opt/ffn-compat/opt/ffn/fe100-csr.json'))}
            if fe.read32(registers['prom_chip_rev_num']) >> 24:
                raise RuntimeError('PCA decoder requires FE100, not FE101')
            for block in ('lif','dfp','fwd'):
                addr = registers[block+'_cr_mode']
                value = fe.read32(addr)
                if value & 8: raise RuntimeError('PCA capture already in use')
                modes[addr] = value
                # Owner pdt: cap_vld is W1C; leave fatal/nonfatal W1C bits zero.
                fe.write32(addr, (value & ~0x3078) | 0x78)
        if put(0, entry, 3):
            raise RuntimeError('DIRECT LIF insert failed')
        rb = (C.c_ubyte * 36)()
        if get(0, rb, 3) or bytes(rb)[4:16] != wanted[4:16]:
            raise RuntimeError('DIRECT LIF readback failed')
        print(json.dumps({'test':'fe100-direct-lif-ready','seconds':20}), flush=True)
        time.sleep(20)
        if capture:
            snapshots = {}
            for block in ('lif','dfp','fwd'):
                if not fe.read32(registers[block+'_cr_mode']) & 64:
                    snapshots[block] = {'valid':False}
                    continue
                words = []
                for pca in range(3):
                    value = 0
                    for n in range(16):
                        value = (value << 32) | fe.read32(registers[f'{block}_ovr_pca{pca}_{n}'])
                    words.append(value)
                # Owner FE100/FE100_A1 model (FE101 has different offsets).
                fields = {'bypass_st':(0,391,383),'in_port':(0,366,361),
                          'pt':(0,319,318),'mode':(0,324,322),'pinit':(0,321,320),
                          'etype':(1,323,308),'fwd_type':(1,124,121),
                          'fwd_key':(1,120,105),'nhidx':(1,47,32),'P':(1,198,198),'F':(1,197,197)}
                snapshots[block] = {name:(words[pca]>>low)&((1<<(high-low+1))-1)
                                    for name,(pca,high,low) in fields.items()}
                snapshots[block]['valid'] = True
                snapshots[block]['raw'] = [f'{v:0128x}' for v in words]
            print(json.dumps({'test':'fe100-pca-capture','snapshots':snapshots}),flush=True)
    finally:
        capture_errors=[]
        if fe:
            for addr,value in modes.items():
                try: fe.write32(addr, value & ~0x3040)
                except Exception as error: capture_errors.append(str(error))
            fe.close()
        if put(0, original, 3):
            raise RuntimeError('original LIF restore failed')
        rb = (C.c_ubyte * 36)()
        if get(0, rb, 3) or bytes(rb)[4:] != before[4:]:
            raise RuntimeError('original LIF restore readback failed')
        print(json.dumps({'test':'fe100-original-lif-restored','passed':True}), flush=True)
        if capture_errors: raise RuntimeError('PCA capture restore errors: '+str(capture_errors))


if __name__ == '__main__':
    main()
