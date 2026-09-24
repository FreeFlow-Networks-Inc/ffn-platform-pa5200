#!/usr/bin/env python3
"""MP orchestration for independent native CP/DP reset services."""
import argparse
import json
from pathlib import Path
import subprocess
import time
from native_plane_boot import profile, preflight, boot, exclusive

STATE=Path('/var/lib/ffn-ngfw/pa5200-restart-owner')


def save(role,state):
    STATE.mkdir(parents=True,exist_ok=True)
    p=STATE/(role+'.json');tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(state,indent=2));tmp.chmod(0o600);tmp.replace(p)


def remote(program,*args,timeout=35):
    # No request can select an SSH destination, program or arbitrary argument.
    if program not in ('plane_restart_node.py','native_plane_boot.py'):raise ValueError('Unknown restart helper')
    argv=['/usr/bin/ssh','-F','/etc/ffn-ngfw/ssh-cp.conf','-o','BatchMode=yes','-o','ConnectTimeout=5',
          'ffn-cp','/usr/bin/python3 /usr/local/sbin/'+program+' '+' '.join(args)]
    p=subprocess.run(argv,capture_output=True,text=True,timeout=timeout,check=True)
    return json.loads(p.stdout.strip().splitlines()[-1])


def inspect(role):
    selected=preflight(profile('cp')) if role=='cp' else remote('native_plane_boot.py','dp','preflight')
    before={r:remote('plane_restart_node.py','observe',r) for r in ('cp','dp')}
    if selected['notes_sha256']!=before[role]['notes_sha256']:
        raise ValueError('Selected kernel differs from the running image; use image activation instead')
    return {'role':role,'selected':selected,'before':before}


def active_owners():
    p=subprocess.run(['systemctl','list-units','--state=active','--no-legend','--plain',
                      'ffn-configd.service','ffn-aggregate@*.service','ffn-vif-links.service','ffn-vif-links.timer'],
                     text=True,capture_output=True,timeout=10,check=True)
    return [line.split()[0] for line in p.stdout.splitlines() if line.strip()]


def execute(role):
    with exclusive('/run/ffn-pa5200-restart-owner.lock'):
        state=inspect(role);state.update(phase='preflight',units=active_owners(),started_at=time.time())
        save(role,state)
        try:
            for unit in state['units']:subprocess.run(['systemctl','stop',unit],check=True,timeout=90)
            state['phase']='preparing';save(role,state)
            remote('plane_restart_node.py','prepare',role,timeout=360)
            state['phase']='booting';save(role,state)
            if role=='cp':boot(profile('cp'))
            else:
                # CP systemd owns the operation even if SSH disconnects.
                subprocess.run(['/usr/bin/ssh','-F','/etc/ffn-ngfw/ssh-cp.conf','ffn-cp',
                                'systemctl start --no-block ffn-native-dp-boot.service'],check=True,timeout=15)
            state['phase']='waiting-for-boot';save(role,state)
            deadline=time.monotonic()+600
            while time.monotonic()<deadline:
                try:
                    after=remote('plane_restart_node.py','observe',role)
                    if after['boot_id']!=state['before'][role]['boot_id'] and after['notes_sha256']==state['selected']['notes_sha256']:
                        break
                except (subprocess.SubprocessError,OSError,ValueError,IndexError):pass
                time.sleep(3)
            else:raise RuntimeError('Selected processor did not return with the pinned kernel')
            state['phase']='restoring';save(role,state)
            remote('plane_restart_node.py','restore',role,timeout=1200)
            other='dp' if role=='cp' else 'cp'
            observed_other=remote('plane_restart_node.py','observe',other)
            if observed_other['boot_id']!=state['before'][other]['boot_id']:
                raise RuntimeError('Other processor boot changed; restoration needs review')
            for unit in reversed(state['units']):subprocess.run(['systemctl','start',unit],check=True,timeout=90)
            state.update(phase='returned',after=after,other_processor_unchanged=True,finished_at=time.time())
            save(role,state);return state
        except Exception as e:
            if state['phase'] in ('preflight','preparing'):
                # No boot command was dispatched: restore the units we paused.
                try:
                    if state['phase']=='preparing':remote('plane_restart_node.py','restore',role,timeout=1200)
                    for unit in reversed(state['units']):subprocess.run(['systemctl','start',unit],check=True,timeout=90)
                    state['pre_reset_restored']=True
                except Exception as recovery:state['restore_error']=str(recovery)
            # Do not replay configuration or re-enable BAR writers after an
            # uncertain hardware reset. The journal retains the restore list.
            state.update(phase='failed',error=str(e),finished_at=time.time());save(role,state)
            raise


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('role',choices=['cp','dp']);p.add_argument('action',choices=['preflight','restart']);a=p.parse_args()
    try:print(json.dumps(inspect(a.role) if a.action=='preflight' else execute(a.role)))
    except Exception as e:p.exit(1,str(e)+'\n')
