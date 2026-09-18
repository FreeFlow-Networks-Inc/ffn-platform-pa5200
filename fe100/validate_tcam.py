#!/usr/bin/env python3
"""Validate recovered TCAM configuration and guards; no replay or reset.

Run on CP using the installed FE100 library environment. Three fresh-process
readback passes also audit native MMIO traces for unintended writes.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from ffn_fe100_external_tables import cfg4_v4_v6

ROOT=Path('/var/lib/ffn/fe100')
HELPERS=Path('/usr/local/sbin')


def run(helper,*args):
    return subprocess.run([sys.executable,str(HELPERS/helper),*args],
        capture_output=True,text=True,timeout=40)


def status():
    child=run('ffn_fe100_hardware_status.py')
    if child.returncode: raise RuntimeError('hardware status failed: '+child.stderr[-1000:])
    return json.loads(child.stdout)


def audit_trace(path):
    data=Path(path).read_text()
    if 'DENIED' in data: raise RuntimeError('MMIO adapter denied an access')
    writes=[tuple(int(v,16) for v in line.split()[1:]) for line in data.splitlines() if line.startswith('W ')]
    expected=[]
    for entry in cfg4_v4_v6():
        expected.extend(((0x80504,entry.address),(0x80500,0x02000201)))
    if writes!=expected: raise RuntimeError('unexpected writes or incomplete TCAM verification trace')
    return {'read_commands':263,'configuration_data_writes':0,'reset_writes':0,
            'denied_accesses':0,'trace_sha256':hashlib.sha256(data.encode()).hexdigest()}


def rejected_guard(helper,*args):
    journal=ROOT/'external-tables.json'
    original=journal.read_bytes()
    traces=set(ROOT.glob('clock-init-*.txt'))
    child=run(helper,*args)
    new=set(ROOT.glob('clock-init-*.txt'))-traces
    if child.returncode==0: raise RuntimeError('destructive operation was unexpectedly accepted')
    if journal.read_bytes()!=original: raise RuntimeError('rejected operation changed journal')
    if not new: raise RuntimeError('guard test did not reach the installed hardware helper')
    for path in new:
        if any(line.startswith(('W ','DENIED')) for line in path.read_text().splitlines()):
            raise RuntimeError('rejected operation attempted MMIO writes')
    message=child.stderr.strip().splitlines()[-1]
    expected=('external-table journal exists' if helper=='ffn_fe100_ddr.py'
              else 'not the known same-boot commissioning failure')
    if expected not in message: raise RuntimeError('guard rejected for unexpected reason: '+message)
    return {'rejected':True,'writes':0,'journal_unchanged':True,'reason':message}


def main():
    report={'started_at_ns':time.time_ns(),'scope':'TCAM configuration and recovery guards',
            'forwarding_verified':False,'session_offload_verified':False}
    output=ROOT/('tcam-validation-'+str(report['started_at_ns'])+'.json')
    try:
        report['before']=status()
        report['passes']=[]
        for number in range(1,4):
            child=run('ffn_fe100_external_runtime.py','--verify')
            if child.returncode: raise RuntimeError('readback failed: '+child.stderr[-1500:])
            result=json.loads(child.stdout)
            if result.get('table_configuration_verified') is not True or result.get('entries_verified')!=263:
                raise RuntimeError('unexpected verification result')
            report['passes'].append(dict(result,pass_number=number,audit=audit_trace(result['trace'])))
        report['guards']={
            'ddr_memory_test':rejected_guard('ffn_fe100_ddr.py','--verify-memory'),
            'repeat_recovery':rejected_guard('ffn_fe100_tcam_recovery.py','--recover','--replay')}
        report['after']=status()
        for field in ('ddr_training_verified','ddr_clocks_ready','tcam_clocks_ready','tcam_synchronized'):
            if report['after'].get(field) is not True: raise RuntimeError('lost hardware prerequisite: '+field)
        if report['after']['recovery_required']: raise RuntimeError('recovery flag is set')
        if report['before']['registers']!=report['after']['registers']:
            raise RuntimeError('DDR/clock/reset registers changed during validation')
        report['passed']=True
    except Exception as error:
        report.update(passed=False,error=str(error))
    output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'passed':report['passed'],'report':str(output),
                      'passes_completed':len(report.get('passes',[])),
                      'error':report.get('error')}))
    return 0 if report['passed'] else 1


if __name__=='__main__': sys.exit(main())
