#!/usr/bin/env python3
"""Select PA5200 controld channels after installing the core and agent code.

Run on MP with --identity /absolute/ssh/key. Does not start/restart services.
CP needs ffn_cp_agent.py; DP needs ffn_dp_agent.py. Both require core's
ffn_agent_protocol.py and ffn_planed.py in /usr/local/lib/ffn. FE100 uses the
existing commissioned non-clearing CSR reader and its local CSR description.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import time


def configuration():
    key='/run/credentials/ffn-controld.service/plane-agent-key'
    common=['/usr/bin/ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
            '-o','ConnectTimeout=5','-o','ServerAliveInterval=3','-o','ServerAliveCountMax=2','-i',key]
    cp=common+['-F','/etc/ffn-ngfw/ssh-cp.conf','ffn-cp',
               'python3 /usr/local/sbin/ffn_cp_agent.py stream']
    dp=common+['-o','IdentitiesOnly=yes','-o','UserKnownHostsFile=/etc/ffn-ngfw/plane_boot_known_hosts',
        '-o','ProxyCommand=/usr/bin/ssh -F /etc/ffn-ngfw/ssh-cp.conf -i '+key+' -W %h:%p ffn-cp',
        'root@127.1.2.2','python3 /usr/local/sbin/ffn_dp_agent.py stream']
    return {'worker_socket':'/run/ffn-plane-mp/control.sock', 'agents':{
        role:{'argv':argv,'role':role,'platform':'pa5200','interval':10,'timeout':25,'stale_after':40}
        for role,argv in (('cp',cp),('dp',dp))}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--identity',required=True)
    args=parser.parse_args()
    identity=Path(args.identity)
    if os.geteuid()!=0 or not identity.is_absolute() or not identity.is_file():
        raise SystemExit('root and an existing absolute SSH identity path required')
    if any(c in str(identity) for c in '\n\r%:"') or any(c.isspace() for c in str(identity)):
        raise SystemExit('identity path contains systemd syntax')
    extension=Path('/opt/ffn-platforms/pa5200-management')
    if json.loads((extension/'extension.json').read_text())['id']!='pa5200':
        raise SystemExit('PA5200 extension must be selected')
    config=Path('/etc/ffn/planes/mp.json')
    worker=json.loads(config.read_text())
    if worker.get('role')!='mp' or worker.get('peer'):
        raise SystemExit('local MP execution worker required')
    worker['commands']['hardware']={a:['/opt/ffn-ngfw-v2/venv/bin/python',str(extension/'hardware_control.py'),a]
                                     for a in ('status','validate','apply')}
    worker['commands']['fe100-policy']={a:['/opt/ffn-ngfw-v2/venv/bin/python',str(extension/'daemon_backend.py'),'fe100-policy',a]
                                       for a in ('status','validate','apply')}
    worker['commands']['wan-path']={a:['/opt/ffn-ngfw-v2/venv/bin/python',str(extension/'wan_backend.py'),a]
                                   for a in ('status','validate','apply')}
    for resource,actions in [('nat',('status','validate','apply')),('dataplane-tools',('status',))]:
        worker['commands'][resource]={a:['/opt/ffn-ngfw-v2/venv/bin/python',str(extension/'nat_backend.py'),resource,a] for a in actions}
    backup=Path('/var/backups/ffn/control-channel-'+str(time.time_ns()))
    def write(path,data,mode=0o644):
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists():
            saved=backup/path.relative_to('/');saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,saved)
        temp=path.with_name(path.name+'.control-new');temp.write_text(data);temp.chmod(mode);temp.replace(path)
    write(config,json.dumps(worker,indent=2)+'\n',0o600)
    write('/etc/ffn/controld.json',json.dumps(configuration(),indent=2)+'\n',0o600)
    # systemd exposes only this credential to the service. ProtectHome remains
    # enabled and private SSH keys never enter the source or telemetry stream.
    write('/etc/systemd/system/ffn-controld.service.d/30-control-channel.conf',
          '[Service]\nSupplementaryGroups=ffn-mgmt\nLoadCredential=plane-agent-key:'+str(identity)+'\n')
    write('/etc/systemd/system/ffn-manager-v2.service.d/30-control-channel.conf',
          '[Service]\nEnvironment=FFN_CONTROL_GATEWAY=controld\nEnvironment=FFN_PLANE_SOCKET=/run/ffn-plane-mp/control.sock\n')
    print(json.dumps({'configured':True,'backup':str(backup),'restart_required':
                      ['ffn-plane@mp','ffn-controld','ffn-manager-v2'],'reboot_required':False}))


if __name__=='__main__':main()
