#!/usr/bin/env python3
"""CP-local restart preparation and challenged, read-only DP observations."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid
from native_plane_boot import profile, sha

STATE = Path('/var/lib/ffn/plane-restart')
CP_UNITS = ('ffn-management-i2c.service','ffn-thermal.service','ffn-bcmd.service',
            'ffn-front-ports.service','ffn-copper.service','ffn-fe100-links.service')


def save(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(data));temp.chmod(0o600);temp.replace(path)


def observe(role, owned=False):
    if role=='cp':
        return {'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'kernel_release':os.uname().release,'notes_sha256':sha(Path('/sys/kernel/notes').read_bytes()),
                'control_ready':True,'ready':os.readlink('/proc/1/exe') in ('/usr/lib/systemd/systemd','/lib/systemd/systemd')}
    nonce=uuid.uuid4().hex
    # Markers are formatted at execution, so the interactive shell's echo cannot
    # be mistaken for a report. Nothing in this command comes from a UI request.
    cmd="printf '\\nBEGIN-%s\\n' '%s'; cat /proc/sys/kernel/random/boot_id; uname -r; sha256sum /sys/kernel/notes; readlink /proc/1/exe; cat /proc/1/root/etc/os-release 2>/dev/null; printf '\\nEND-%s\\n' '%s'" % ('%s',nonce,'%s',nonce)
    if owned:
        # Only the reset executor calls this while holding the exclusive fence.
        import importlib.util, io
        from contextlib import redirect_stdout
        spec=importlib.util.spec_from_file_location('owned_dpsh','/usr/local/sbin/ffn_dpsh2.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        session=module.Session(program_window=False)
        try:
            if session.check():raise ValueError('DP recovery mailbox is not ready')
            output=io.StringIO()
            with redirect_stdout(output):code=module.one_shot(session,cmd,8)
            if code:raise ValueError('DP observation failed')
            text=output.getvalue()
        finally:session.close()
    else:
        p=subprocess.run(['/usr/bin/python3','/usr/local/sbin/ffn_dpsh2.py','--skip-window','-t','8','-c',cmd],
                         capture_output=True,text=True,timeout=15,check=True)
        text=p.stdout
    return parse_observation(text,nonce)


def parse_observation(text,nonce):
    text=re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]','',text).replace('\r','')
    begin='\nBEGIN-'+nonce+'\n';end='\nEND-'+nonce+'\n'
    if text.count(begin)!=1 or text.count(end)!=1:raise ValueError('No challenged DP report')
    lines=text.split(begin,1)[1].split(end,1)[0].strip().splitlines()
    if len(lines)<4 or not lines[2].split():raise ValueError('Incomplete DP boot report')
    boot=str(uuid.UUID(lines[0]));notes=lines[2].split()[0]
    if boot!=lines[0] or not re.fullmatch('[0-9a-f]{64}',notes):raise ValueError('Invalid DP boot identity')
    ready=lines[3] in ('/usr/lib/systemd/systemd','/lib/systemd/systemd') and any(x in ('ID=debian','ID="debian"') for x in lines[4:])
    return {'boot_id':boot,'kernel_release':lines[1],'notes_sha256':notes,'control_ready':True,
            'ready':ready,'runtime':'debian-systemd' if ready else 'recovery', 'forwarding_verified':False}


def active(unit):
    return subprocess.run(['systemctl','is-active','--quiet',unit],capture_output=True,timeout=10).returncode==0


def wait_dp_owner():
    deadline=time.monotonic()+180
    while time.monotonic()<deadline:
        p=subprocess.run(['systemctl','show','ffn-native-dp-boot.service',
                          '--property=ActiveState,Result,ExecMainStatus'],
                         capture_output=True,text=True,check=True,timeout=10)
        status=dict(line.split('=',1) for line in p.stdout.splitlines() if '=' in line)
        if status.get('ActiveState') in ('activating','deactivating'):
            time.sleep(1);continue
        if status.get('ActiveState')!='inactive' or status.get('Result')!='success' or status.get('ExecMainStatus')!='0':
            raise RuntimeError('DP boot owner did not complete successfully')
        return
    raise RuntimeError('DP boot owner is still running; restoration deferred')


def reconnect_dp(before):
    from native_plane_boot import preflight, exclusive, stop_transport, run
    cfg=profile('dp');preflight(cfg)
    with exclusive('/run/ffn-dp-reset.lock'):
        stop_transport(cfg)
        enable=Path('/sys/bus/pci/devices')/cfg['pci']/'enable'
        if enable.read_text().strip()=='0':enable.write_text('1')
        # CP enumeration clears the DP's PCIe window, not its running DRAM.
        # Restore only the mailbox/transport window; never reset the DP here.
        run([cfg['tools']['csr']['path'],'--devnum='+str(cfg['devnum']),
             'PEM0_BAR1_INDEX1','0x11'],env=dict(os.environ,**cfg.get('environment',{})),hardware=True)
        after=observe('dp',owned=True)
        if any(after[key]!=before[key] for key in ('boot_id','notes_sha256')):
            raise RuntimeError('DP boot changed during CP restart; transport remains stopped')
        run(['systemctl','start',cfg['transport']['unit']],timeout=30)
    return after


def prepare(role):
    before={r:observe(r) for r in ('cp','dp')}
    units=[u for u in CP_UNITS if active(u)] if role=='cp' else []
    state={'role':role,'units':units,'before':before,'created_at':time.time(),'restored':False}
    save(STATE/(role+'.json'),state)
    if role=='cp':
        Path('/run/ffn-cp-restart.pending').write_text('preparing\n')
        # The thermal service's stop handler sets full PWM before the CP goes away.
        for unit in reversed(units):subprocess.run(['systemctl','stop',unit],check=True,timeout=90)
        subprocess.run(['/usr/bin/python3','/usr/local/sbin/ffn_thermal.py','full'],check=True,timeout=20)
        from native_plane_boot import stop_transport
        stop_transport(profile('dp'))
    return state


def restore(role):
    state=json.loads((STATE/(role+'.json')).read_text())
    if state['role']!=role:raise ValueError('Recovery journal mismatch')
    if role=='dp':wait_dp_owner()
    if role=='cp':
        for unit in state['units']:
            if unit not in CP_UNITS:raise ValueError('Unexpected recovery unit')
            subprocess.run(['systemctl','start',unit],check=True,timeout=1120)
            if not active(unit):raise RuntimeError('Restored service is not active: '+unit)
        reconnect_dp(state['before']['dp'])
    transport=profile('dp')['transport']['unit']
    for _ in range(3):
        if not active(transport):raise RuntimeError('DP transport is not active after restart')
        time.sleep(1)
    if role=='cp':
        Path('/run/ffn-cp-restart.pending').unlink(missing_ok=True)
    state['restored']=True;save(STATE/(role+'.json'),state)
    return state


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['observe','prepare','restore']);p.add_argument('role',choices=['cp','dp']);a=p.parse_args()
    try:print(json.dumps({'observe':observe,'prepare':prepare,'restore':restore}[a.action](a.role)))
    except Exception as e:p.exit(1,str(e)+'\n')
