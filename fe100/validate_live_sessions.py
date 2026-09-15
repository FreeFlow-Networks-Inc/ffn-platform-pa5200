#!/usr/bin/env python3
"""Isolated CPU session-table test; never asserts physical packet offload.

Requires empty tables and initialized hardware. Journals exact test keys
before insertion, verifies hit/miss and only removes its own matching entries.
"""
import json
import os
from pathlib import Path
import time
from ffn_fe100_live_sessions import LiveSessions, ROOT
from ffn_fe100_sessions import key4, entry4
from ffn_fe100_session_adapter import encode_native, decode_native, SUCCESS, NOT_FOUND


def main():
    io=LiveSessions(writable=True)
    before=io.status()
    if any(before['registers'][r] for r in ('0x40428','0x40450')):
        raise RuntimeError('isolated validation requires empty flow tables')
    keys=[key4('198.18.0.1','198.18.0.2',40001,40002,17,4094),
          key4('198.18.0.2','198.18.0.1',40002,40001,17,4094)]
    wires=[entry4(k,0x1001+i) for i,k in enumerate(keys)]
    report={'schema':1,'cp_boot_id':before['cp_boot_id'],'stage':'started',
            'keys':[k.hex() for k in keys],'entries':[w.hex() for w in wires],
            'before':before,'operations':[],'session_offload_verified':False,
            'session_table_verified':False,'cleanup_verified':False}
    path=ROOT/('session-validation-'+str(time.time_ns())+'.json')
    with path.open('x') as f: json.dump(report,f);f.flush();os.fsync(f.fileno())
    def save():
        with path.open('w') as f: json.dump(report,f,indent=2);f.flush();os.fsync(f.fileno())
    def call(op,wire):
        rc,native=io.call(op,encode_native(wire))
        report['operations'].append({'operation':op,'key':wire[:16].hex(),'rc':rc,'native':native.hex()})
        save();return rc,native
    touched=[]
    try:
        for wire in wires:
            rc,_=call('fetch',wire)
            if rc!=NOT_FOUND: raise RuntimeError('test key is not verified absent: '+str(rc))
        for wire in wires:
            touched.append(wire)
            report['stage']='inserting';save()
            rc,_=call('insert',wire)
            if rc!=SUCCESS: raise RuntimeError('session insert failed: '+str(rc))
            rc,native=call('fetch',wire)
            if rc!=SUCCESS or decode_native(native,wire[:16])!=wire:
                raise RuntimeError('inserted session readback failed: '+str(rc))
        report['session_table_verified']=True
        report['stage']='removing';save()
    except BaseException as e:
        report.update(stage='failed',error=str(e))
        raise
    finally:
        failures=[]
        for wire in reversed(touched):
            try:
                rc,native=call('fetch',wire)
                if rc==SUCCESS:
                    if decode_native(native,wire[:16])!=wire: raise RuntimeError('entry ownership differs')
                    rc,_=call('delete',wire)
                    if rc!=SUCCESS: raise RuntimeError('delete failed: '+str(rc))
                elif rc!=NOT_FOUND: raise RuntimeError('cleanup fetch failed: '+str(rc))
                rc,_=call('fetch',wire)
                if rc!=NOT_FOUND: raise RuntimeError('removal not confirmed: '+str(rc))
            except Exception as e: failures.append(str(e))
        report.update(after=io.status(),cleanup_errors=failures,
                      cleanup_verified=not failures,faults=io.shim.ffn_fe100_faults())
        if report['session_table_verified'] and not failures:
            report['stage']='completed'
        elif failures: report['stage']='cleanup-required'
        save();print(json.dumps(dict(report,journal=str(path)),indent=2))
        if failures: raise RuntimeError('session cleanup incomplete')


if __name__=='__main__': main()
