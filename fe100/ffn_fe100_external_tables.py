#!/usr/bin/env python3
"""FE100 external TCAM cfg4 IPv4/IPv6 initialization transactions.

Recovered from the hash-pinned owner's fe100_tdi_table_cfg4_v4_v6 at
0x10301320..0x10301658. This is table configuration, not a session entry codec.
No hardware is opened by this module; the caller supplies an exclusive,
bounded IA transport after DDR/TCAM memory commissioning has succeeded.
"""
import ctypes as C
from dataclasses import dataclass
import json


class IndirectAccess(C.Structure):
    _fields_=[('trgt_mem',C.c_uint32),('loop',C.c_uint32),
        ('addr_inc',C.c_uint32),('phy_mode',C.c_uint32),
        ('acc_type',C.c_uint32),('acc_size',C.c_uint32),
        ('addr_sz',C.c_uint32),('addr',C.c_uint64),
        ('dcnt',C.c_uint32),('data',C.POINTER(C.c_uint32)),
        ('cc',C.c_uint32),('hit_idx',C.c_uint32)]


@dataclass(frozen=True)
class Write:
    address: int
    words: tuple

    def native(self):
        if C.sizeof(IndirectAccess)!=64 or IndirectAccess.data.offset!=48:
            raise RuntimeError('external lookup ABI requires a 64-bit process')
        if len(self.words)!=3 or not all(type(w) is int and 0<=w<=0xffffffff for w in self.words):
            raise ValueError('external TCAM entry must contain three 32-bit words')
        if type(self.address) is not int or not 0<=self.address<=0xffffffff:
            raise ValueError('external TCAM address outside audited range')
        data=(C.c_uint32*3)(*self.words)
        # fe100_ia_t DWARF: trgt_mem at0, acc_type16, acc_size20,
        # addr32, dcnt40, data48. Values match the owner's native call.
        request=IndirectAccess(trgt_mem=0x100,acc_type=2,acc_size=3,
                               addr=self.address,dcnt=3,data=data)
        return request,data  # keep data alive throughout the synchronous call


def cfg4_v4_v6():
    result=[Write(1,(0,0,0x08000042))]
    result.extend(Write(0x1000+32*i,(0,0,5)) for i in range(256))
    result.extend(Write(0x40a00+i,(0,0xffffffff,0xffffffff)) for i in range(4))
    result.append(Write(0x40a5a,(0,0,0xfff)))
    result.append(Write(0x40a5c,(0,0,0xe83c8780)))
    return tuple(result)


def initialize(transport):
    """Apply through an already locked transport with live readiness checks.

    A failed or interrupted transaction leaves the partition unqualified;
    repeating a destructive table initialization is never automatic. The
    transport must persist begin/failure/complete state for reboot recovery.
    """
    status=transport.status()
    for field in ('exclusive','ddr_training_verified','ddr_memory_test_verified',
                  'tcam_ready','packet_lookup_quiescent'):
        if status.get(field) is not True:
            raise RuntimeError('external tables require '+field)
    if status.get('initialization_started'):
        raise RuntimeError('external table initialization already started; inspect recovery state')
    transport.begin('cfg4-v4-v6',263)
    completed=0
    try:
        for entry in cfg4_v4_v6():
            request,data=entry.native()
            rc=transport.ia_op(0,16,request)
            if rc:
                raise RuntimeError('TCAM configuration failed at '+hex(entry.address)+': '+str(rc))
            completed+=1
        # Hardware readback/lookup tests are required before a completion mark.
        if transport.verify_configuration(cfg4_v4_v6()) is not True:
            raise RuntimeError('external table configuration verification failed')
        transport.complete(completed)
    except BaseException:
        transport.failed(completed)
        raise
    return {'transactions':completed,'table_configuration_verified':True,
            'session_offload_verified':False}


if __name__=='__main__':
    print(json.dumps({'mode':'plan','block':16,'transactions':[
        {'address':hex(w.address),'words':[hex(v) for v in w.words]} for w in cfg4_v4_v6()],
        'hardware_applied':False},indent=2))
