#!/usr/bin/env python3
"""Install/read back the local owner parser JSON through its verified ABI.

Only replaces the known zero-action commissioning table, or restores the
captured original table. No global reset or table flush is used.
"""
import argparse
import ctypes as C
import hashlib
import json
from pathlib import Path
import struct
from ffn_fe100_parser_inspect import open_parser

BACKUP=Path('/var/lib/ffn/fe100/parser-before-direct-encode.json')
SOURCE=Path('/opt/ffn-compat/tmp/dpfs/etc/fe-parser.json')
FIELDS={
    'exccode':(8,16,8),'sysport':(8,0,16),
    'pkey0_idx':(24,19,5),'pkey0_en':(24,18,1),'pkey1_idx':(24,13,5),'pkey1_en':(24,12,1),
    'proto_en':(24,11,1),'chk_en':(24,10,1),'acl_en':(24,9,1),'fnf2lpp':(24,8,1),
    'snoop':(24,7,1),'mode':(24,4,3),'pinit':(24,2,2),'pt':(24,0,2),
    'o_adjust':(28,28,4),'o_adjust_sub':(28,27,1),'o_mask':(28,19,8),'o_shift':(28,16,3),
    'o_shift_r':(28,15,1),'o_idx':(28,10,5),'o_select':(28,8,2),'next':(28,3,5),
    'action':(28,1,2),'valid':(28,0,1)}
for n in range(4):
    offset=12+(n//2)*4
    FIELDS[f'fkey{n}_mask']=(offset,20 if n%2==0 else 6,8)
    FIELDS[f'fkey{n}_idx']=(offset,15 if n%2==0 else 1,5)
    FIELDS[f'fkey{n}_en']=(offset,14 if n%2==0 else 0,1)
    FIELDS[f'lkey{n}_idx']=(20,19-6*n,5)
    FIELDS[f'lkey{n}_en']=(20,18-6*n,1)


def encode(c):
    if set(c)-set(FIELDS)-{'key','mask'}: raise ValueError('unknown parser setting')
    words=[0]*8
    for name,value in {**c,'valid':1}.items():
        if name in ('key','mask'): continue
        offset,low,width=FIELDS[name]
        if type(value) is not int or not 0<=value<1<<width: raise ValueError('invalid '+name)
        words[offset//4]|=value<<low
    for n,name in enumerate(('key','mask')):
        k=c.get(name,{})
        if set(k)-{'stage','pkey0','pkey1','valid'}: raise ValueError('unknown parser key')
        words[n]=1<<31
        for field,low,width in [('stage',16,5),('pkey0',8,8),('pkey1',0,8)]:
            v=k.get(field,0)
            if type(v) is not int or not 0<=v<1<<width: raise ValueError('invalid key '+field)
            words[n]|=v<<low
    # TCAM readback canonicalizes don't-care key bits to zero.
    words[0] &= words[1]
    return struct.pack('>8I',*words)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['apply','restore'])
    args=p.parse_args()
    source=SOURCE.read_bytes()
    entries=json.loads(source)['pan_fe100_parse_table']
    if set(entries)!={str(i) for i in range(55)}: raise ValueError('unexpected parser table indices')
    wanted=[encode(entries[str(i)]) for i in range(55)]
    lock,shim,lib,get,put=open_parser()
    def fetch(i):
        b=(C.c_ubyte*32)()
        rc=get(0,b,i)
        if rc not in (0,3): raise RuntimeError('fetch failed '+str(rc))
        return bytes(b)
    before=[fetch(i) for i in range(55)]
    zero=bytes.fromhex('8000000080000000000000000000000000000000000000000000000000000001')
    if args.action=='apply':
        if before==wanted:
            print(json.dumps({'already_applied':True}));return
        if any(b!=zero for b in before): raise RuntimeError('refusing to replace an unexpected parser table')
        if BACKUP.exists():
            saved=json.loads(BACKUP.read_text())
            if saved['entries']!=[b.hex() for b in before]: raise RuntimeError('backup differs from baseline')
        else:
            with BACKUP.open('x') as f:
                json.dump({'source_sha256':hashlib.sha256(source).hexdigest(),'entries':[b.hex() for b in before]},f)
                f.flush()
                import os
                os.fsync(f.fileno())
    else:
        if before!=wanted: raise RuntimeError('parser differs from installed reference; inspect before restoring')
        saved=json.loads(BACKUP.read_text())
        wanted=[bytes.fromhex(x) for x in saved['entries']]
        if len(wanted)!=55 or any(b!=zero for b in wanted): raise ValueError('invalid original table backup')
    touched=[]
    try:
        for i,b in enumerate(wanted):
            touched.append(i)
            rc=put(0,(C.c_ubyte*32).from_buffer_copy(b),i)
            actual=fetch(i)
            if rc or actual!=b: raise RuntimeError(f'parser write/readback failed at {i}: rc={rc} wanted={b.hex()} actual={actual.hex()}')
    except BaseException as original:
        errors=[]
        for i in reversed(touched):
            rc=put(0,(C.c_ubyte*32).from_buffer_copy(before[i]),i)
            if rc or fetch(i)!=before[i]: errors.append(i)
        raise RuntimeError(f'parser update failed: {original}; rollback failures: {errors}') from original
    print(json.dumps({'action':args.action,'entries_verified':55,'source_sha256':hashlib.sha256(source).hexdigest(),'passed':True}))


if __name__=='__main__': main()
