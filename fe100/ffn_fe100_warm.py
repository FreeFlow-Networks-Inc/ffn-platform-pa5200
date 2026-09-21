#!/usr/bin/env python3
"""Fresh retained-state qualification for isolated commissioning only.

Never copy old boot IDs into current training journals, retrain DDR, reset
hardware, clear sessions, or grant production admission. A bounded lab grant
expires and is invalidated by source, profile, boot or stable CSR changes.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
from ffn_fe100_clocks import SHA
from ffn_fe100_ddr_diagnostics import stable_snapshot,UMCTL_STATUS

TTL=600
SCOPE='isolated-commissioning'
STABLE=(0x40010,0x40014,0x40400,0x40404,0x48008,0x48018,0x48708,
        0xa8100,0xa8104,0xa8134,0xa8148,0xa8150,0xa8168,0xa8170,
        0xb0100,0xb0104,0xb0134,0xb0148,0xb0150,0xb0168,0xb0170,
        0x98008,0x98174,0x98128,0x98130,0x98148,0x98150,0x78134,0x78804)


def fingerprint(path):
    st=path.stat()
    if st.st_uid!=0 or st.st_mode&0o022 or path.is_symlink():raise ValueError('Untrusted commissioning evidence')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def calibration_errors(report):
    errors=[];expected={'fcm':(0,1),'fhm':(3,4),'fdt':(5,6)}[report['block']]
    if (report.get('faults')!=0 or report.get('memory_written') is not False or
        stable_snapshot(report.get('before',{}))!=stable_snapshot(report.get('after',{})) or
        tuple(c['channel'] for c in report['channels'])!=expected):
        return ['Incomplete or changed diagnostic observation']
    for channel in report['channels']:
        # Audited check_init_cal_status: calibration state nibble must be8,
        # no calibration/per-bit errors. Do not invoke its unbounded poll loop.
        c=channel['calibration_registers']
        if c['0x18']!=0 or (c['0x19']>>12)&15!=8:errors.append('Calibration state/error for channel '+str(channel['channel']))
        groups=channel['groups'];count=4 if channel['channel'] in (5,6) else 1
        if [g['group'] for g in groups]!=list(range(count)):errors.append('Incomplete PHY groups')
        for group in groups:
            r=group['registers']
            if r['0x14']!=0 or r['0x17']!=0xf080 or group['measurements_present'] is not True:
                errors.append('PHY training/error/measurement mismatch')
    return errors


def grant_valid(root,boot,values,now=None):
    """Consumed only by the isolated lab; never by production adapters."""
    try:
        path=root/('warm-lab-'+boot+'.json');fingerprint(path);record=json.loads(path.read_text())
        age=(time.monotonic() if now is None else now)-record['monotonic']
        if (record['schema']!=1 or record['stage']!='verified' or record['cp_boot_id']!=boot or
            record['scope']!=SCOPE or record['owner_sha256']!=SHA or record['production_admission'] is not False or
            not 0<=age<=TTL or record['stable']!=stable_snapshot({hex(r):values[r] for r in STABLE})):return False
        from ffn_fe100_config import load_profile
        from dataclasses import asdict
        if record['profile']!=asdict(load_profile()):return False
        if not record['sources']:return False
        for name,digest in record['sources'].items():
            if Path(name).name!=name or fingerprint(root/name)!=digest:return False
        return True
    except (OSError,ValueError,KeyError,TypeError):return False


def issue():
    from ffn_fe100_live_sessions import LiveSessions,ROOT,calibration_journals,flu_record,prerequisites,action_prerequisites
    from ffn_fe100_config import load_profile
    from dataclasses import asdict
    io=LiveSessions();before=io.status();boot=before['cp_boot_id'];values={int(k,16):v for k,v in before['registers'].items()}
    path=ROOT/('warm-lab-'+boot+'.json')
    # Invalidate an earlier grant before beginning any new inspection.
    record=dict(schema=1,scope=SCOPE,stage='started',cp_boot_id=boot,owner_sha256=SHA,
                production_admission=False,monotonic=time.monotonic(),sources={})
    def save():
        temp=path.with_suffix('.new')
        with temp.open('w') as f:
            os.chmod(temp,0o600);json.dump(record,f,indent=2);f.flush();os.fsync(f.fileno())
        os.replace(temp,path)
        fd=os.open(ROOT,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    save()
    try:
        if values[0x40428] or values[0x40450]:raise ValueError('Warm qualification requires empty hardware sessions')
        if any(values[r]&0x2000000f!=1 for r in UMCTL_STATUS):raise ValueError('Memory controller state or sticky stall fault')
        # Never override a failed or partial initialization attempted this boot.
        patterns=('fhm-','fdt-train-','fdt-recover-','flu-init-','flu-verified-','fcm-','sem-init-')
        if any(p.name.endswith(boot+'.json') and p.name.startswith(patterns)
               for p in ROOT.glob('*.json')):
            raise ValueError('Current-boot commissioning records require their normal recovery path')
        candidates=[]
        for source in ROOT.glob('flu-verified-*.json'):
            old=source.name[len('flu-verified-'):-5]
            try:
                if str(uuid.UUID(old))!=old or old==boot:continue
                journals=calibration_journals(ROOT,old)
                if prerequisites(values,old,journals) or not flu_record(ROOT,old) or action_prerequisites(values,ROOT,old):continue
                paths=[Path(r['journal']) for r in journals.values()]+[source]
                paths += [ROOT/(s+'-'+old+'.json') for s in ('fcm-clocks','fcm-train-0','fcm-train-1','sem-init')]
                hashes={p.name:fingerprint(p) for p in paths}
                candidates.append((old,hashes))
            except (OSError,ValueError,KeyError):continue
        if len(candidates)!=1:raise ValueError('Exactly one intact retained-state provenance chain required')
        record['source_boot'],record['sources']=candidates[0]
        record['profile']=asdict(load_profile());record['diagnostics']=[];save()
        ld='/opt/ffn-compat/tmp/dpfs/usr/local/lib64:/opt/ffn-compat/tmp/dpfs/usr/local/lib64/3p:/opt/ffn-compat/tmp/dpfs/usr/lib64'
        for block in ('fhm','fdt','fcm'):
            p=subprocess.run(['python3','/usr/local/sbin/ffn_fe100_ddr_diagnostics.py','--block',block],
                env=dict(os.environ,LD_LIBRARY_PATH=ld,FFN_FE100_LOCK_FD=str(io.lock.fileno())),
                pass_fds=(io.lock.fileno(),),capture_output=True,text=True,timeout=30)
            if p.returncode:raise ValueError('PHY diagnostic failed: '+p.stderr[-512:])
            d=json.loads(p.stdout);record['diagnostics'].append(d);save()
            if d['cp_boot_id']!=boot or calibration_errors(d):raise ValueError('PHY readback did not qualify: '+block)
        after=io.status();latest={int(k,16):v for k,v in after['registers'].items()}
        if (after['cp_boot_id']!=boot or any(latest[r]!=values[r] for r in (0x40428,0x40450)) or
            stable_snapshot({hex(r):latest[r] for r in STABLE})!=stable_snapshot({hex(r):values[r] for r in STABLE})):
            raise ValueError('Hardware changed during qualification')
        if any(fingerprint(ROOT/n)!=h for n,h in record['sources'].items()):raise ValueError('Provenance changed during qualification')
        record.update(stage='verified',monotonic=time.monotonic(),stable=stable_snapshot({hex(r):latest[r] for r in STABLE}),
                      expires_after_seconds=TTL,hardware_reset=False,memory_written=False)
        save();return dict(record,journal=str(path))
    except BaseException as e:
        record.update(stage='failed',error=str(e));save();raise


if __name__=='__main__':
    import sys
    if sys.argv[1:]!=['--qualify-lab']:raise SystemExit('Use --qualify-lab for bounded retained-state diagnostics')
    try:print(json.dumps(issue()))
    except (ValueError,OSError,RuntimeError,subprocess.TimeoutExpired) as e:
        print(json.dumps({'qualified':False,'error':str(e)}));raise SystemExit(2)
