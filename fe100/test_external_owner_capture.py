#!/usr/bin/env python3
"""Compare FFN's external-table plan with native owner calls, with MMIO replaced."""
import argparse
import ctypes as C
import hashlib
import json
import os
from ffn_fe100_external_tables import cfg4_v4_v6
from ffn_fe100_clocks import LIB,SHA


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('capture_library')
    args=p.parse_args()
    with open(LIB,'rb') as f:
        if hashlib.file_digest(f,'sha256').hexdigest()!=SHA:
            raise RuntimeError('owner ABI changed')
    capture=C.CDLL(args.capture_library,mode=os.RTLD_GLOBAL|os.RTLD_NOW)
    owner=C.CDLL(LIB,mode=os.RTLD_LOCAL|os.RTLD_LAZY)
    fn=owner.fe100_tdi_table_cfg4_v4_v6
    fn.argtypes=[C.c_uint32]; fn.restype=C.c_int
    if fn(0): raise RuntimeError('owner configuration capture failed')
    class Record(C.Structure):
        _fields_=[('address',C.c_uint64),('words',C.c_uint32*3)]
    capture.ffn_capture_record.argtypes=[C.c_uint32]
    capture.ffn_capture_record.restype=C.POINTER(Record)
    plan=cfg4_v4_v6()
    if capture.ffn_capture_count()!=len(plan): raise AssertionError('transaction count differs')
    for i,w in enumerate(plan):
        actual=capture.ffn_capture_record(i).contents
        if (actual.address,tuple(actual.words))!=(w.address,w.words):
            raise AssertionError('owner transaction differs at '+str(i))
    print(json.dumps({'owner_transactions_compared':len(plan),'passed':True,'hardware_access':False}))


if __name__=='__main__': main()
