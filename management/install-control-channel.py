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


def wanted_by(unit_text):
    """The targets a unit's [Install] section names in WantedBy=, in order, once each."""
    section=None;targets=[]
    for raw in unit_text.splitlines():
        line=raw.strip()
        if not line or line[0] in '#;':continue
        if line.startswith('['):section=line.strip('[]').strip();continue
        if section=='Install' and '=' in line:
            key,value=(part.strip() for part in line.split('=',1))
            if key=='WantedBy':
                for target in value.split():
                    if target not in targets:targets.append(target)
    return targets


def enable_unit(name,unit_text,system=Path('/etc/systemd/system')):
    """Create the <target>.wants/<unit> links `systemctl enable` would, one per WantedBy= target.

    This installer writes unit files by hand, so it has to make these links by hand too: a
    WantedBy= that is only declared is inert. The feeds that are PartOf= the control daemon
    declared themselves wanted by it, but only the multi-user link was ever made, so the core
    patch installer's `systemctl stop ffn-controld` took them down and its `start` brought
    nothing back (2026-10-20: fifteen minutes without default routes). Returns the links made."""
    made=[]
    for target in wanted_by(unit_text):
        wants=system/(target+'.wants')/name
        wants.parent.mkdir(parents=True,exist_ok=True)
        if not wants.exists() and not wants.is_symlink():
            wants.symlink_to('../'+name);made.append(str(wants))
    return made


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
    worker['commands']['fe100-sessions']={'status':['/opt/ffn-ngfw-v2/venv/bin/python',str(extension/'session_backend.py'),'status']}
    worker['commands']['physical-ports']={a:['/opt/ffn-ngfw-v2/venv/bin/python',str(extension/'physical_backend.py'),a] for a in ('status','lookup','validate','apply')}
    # One resource for every faceplate port (PORT-TABLE.md): the commit's sync is one request that
    # starts the CP owners, pushes the table to ffn-portd and waits for the rows to attach.
    worker['commands']['ports']={a:['/opt/ffn-ngfw-v2/venv/bin/python',str(extension/'ports_backend.py'),a] for a in ('status','lookup','validate','apply')}
    worker.setdefault('timeouts',{})['ports']={a:(150 if a=='apply' else 60) for a in ('status','lookup','validate','apply')}
    worker['commands']['front-traffic']={'status':['/opt/ffn-ngfw-v2/venv/bin/python',str(extension/'daemon_backend.py'),'front-traffic','status']}
    worker['commands']['lldp']={a:['/opt/ffn-ngfw-v2/venv/bin/python',str(extension/'daemon_backend.py'),'lldp',a] for a in ('status','validate','apply')}
    # Device > Setup > Interfaces reads and applies the MP's own ports through the controller
    # (CONSOLE-HARDWARE.md); without this entry the card reports the controller unavailable.
    worker['commands']['mp-interfaces']={a:['/opt/ffn-ngfw-v2/venv/bin/python',str(extension/'mp_interfaces.py'),a] for a in ('status','apply')}
    # A physical-ports lookup is two ssh hops (CP, then DP through the CP) and an attach four or
    # more, each bounded at 45 s in the backend; the daemon's 20 s read / 90 s apply defaults are
    # too tight under replay load. Declared here, checked by the daemon's timeouts() ladder.
    worker.setdefault('timeouts',{})['physical-ports']={a:(100 if a=='apply' else 60) for a in ('status','lookup','validate','apply')}
    worker['commands']['route-links']={'refresh':['/opt/ffn-ngfw-v2/venv/bin/python',str(extension/'daemon_backend.py'),'route-links','refresh']}
    # The convergence check for the dataplane's link-tracked routes (core ffn_convergence.py asks
    # for it as the `tracked-routes` subsystem): one ssh round trip to the DP, read-only.
    worker['commands']['tracked-routes']={'status':['/opt/ffn-ngfw-v2/venv/bin/python',str(extension/'daemon_backend.py'),'tracked-routes','status']}
    worker.setdefault('timeouts',{})['tracked-routes']={'status':40}
    worker['commands']['plane-images']={a:['/opt/ffn-ngfw-v2/venv/bin/python',str(extension/'plane_images.py'),a]
                                       for a in ('status','validate','apply')}
    worker['commands']['plane-lifecycle']={a:['/opt/ffn-ngfw-v2/venv/bin/python',str(extension/'plane_lifecycle.py'),a]
                                          for a in ('status','validate','apply')}
    for resource,actions in [('nat',('status','validate','apply')),('security',('status','validate','apply')),('dataplane-tools',('status',))]:
        worker['commands'][resource]={a:['/opt/ffn-ngfw-v2/venv/bin/python',str(extension/'nat_backend.py'),resource,a] for a in actions}
    backup=Path('/var/backups/ffn/control-channel-'+str(time.time_ns()))
    def write(path,data,mode=0o644):
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists():
            saved=backup/path.relative_to('/');saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,saved)
        temp=path.with_name(path.name+'.control-new');temp.write_text(data);temp.chmod(mode);temp.replace(path)
    write(config,json.dumps(worker,indent=2)+'\n',0o600)
    # The MP relay for the dataplane LLDP agent, beside ffn-dhcp and ffn-protect.
    write('/usr/local/sbin/ffn-lldp',(Path(__file__).parent.parent/'octeon/debian/ffn-lldp-mp').read_text(),0o755)
    # Runtime Python modules alone cannot activate an aggregate. Install its
    # lifecycle unit with the selected MP channel; configd starts only committed
    # definitions, so installing this template does not enable any front port.
    write('/etc/systemd/system/ffn-aggregate@.service',
          Path(__file__).with_name('ffn-aggregate@.service').read_text())
    write('/etc/systemd/system/ffn-fe100-session-feed.service',
          Path(__file__).with_name('ffn-fe100-session-feed.service').read_text())
    write('/etc/systemd/system/ffn-fe100-session-feed.service.d/30-control-channel.conf',
          '[Service]\nLoadCredential=plane-agent-key:'+str(identity)+'\n')
    enable_unit('ffn-fe100-session-feed.service',Path(__file__).with_name('ffn-fe100-session-feed.service').read_text())
    write('/etc/ffn/controld.json',json.dumps(configuration(),indent=2)+'\n',0o600)
    write('/etc/systemd/system/ffn-interface-service-tunnel.service',
          (extension/'ffn-interface-service-tunnel.service').read_text())
    write('/etc/systemd/system/ffn-interface-service-tunnel.service.d/30-control-channel.conf',
          '[Service]\nLoadCredential=plane-agent-key:'+str(identity)+'\n')
    enable_unit('ffn-interface-service-tunnel.service',(extension/'ffn-interface-service-tunnel.service').read_text())
    # systemd exposes only this credential to the service. ProtectHome remains
    # enabled and private SSH keys never enter the source or telemetry stream.
    write('/etc/systemd/system/ffn-controld.service.d/30-control-channel.conf',
          '[Service]\nSupplementaryGroups=ffn-mgmt\nLoadCredential=plane-agent-key:'+str(identity)+'\n')
    write('/etc/systemd/system/ffn-manager-v2.service.d/30-control-channel.conf',
          '[Service]\nEnvironment=FFN_CONTROL_GATEWAY=controld\nEnvironment=FFN_PLANE_SOCKET=/run/ffn-plane-mp/control.sock\n')
    write('/etc/systemd/system/ffn-route-link-feed.service',(extension/'ffn-route-link-feed.service').read_text())
    # The plane journals live in RAM (journald-ffn-volatile.conf); this timer pulls them onto the RAID.
    write('/usr/local/sbin/ffn_log_ship.py',Path(__file__).with_name('ffn_log_ship.py').read_text(),0o755)
    for unit in ('ffn-log-ship.service','ffn-log-ship.timer'):
        write('/etc/systemd/system/'+unit,Path(__file__).with_name(unit).read_text())
    wanted=Path('/etc/systemd/system/timers.target.wants/ffn-log-ship.timer')
    wanted.parent.mkdir(parents=True,exist_ok=True)
    if not wanted.exists() and not wanted.is_symlink(): wanted.symlink_to('../ffn-log-ship.timer')
    enable_unit('ffn-route-link-feed.service',(extension/'ffn-route-link-feed.service').read_text())
    print(json.dumps({'configured':True,'backup':str(backup),'restart_required':
                      ['ffn-plane@mp','ffn-controld','ffn-manager-v2','ffn-fe100-session-feed',
                       'ffn-route-link-feed','ffn-interface-service-tunnel'],'reboot_required':False}))


if __name__=='__main__':main()
