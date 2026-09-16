#!/usr/bin/env python3
"""Install a staged copper VIF driver without enabling packet forwarding.

Stage this file, vif_backend.py, vif-link-poll.py, vif-ui.js, both
ffn-vif-links units, ffn_copper_forwarding.py, the three DP driver modules and
ffn_inspection.py together on the MP.
Requires an existing VIF installation, stopped transport and empty assignments.
Discovers verified wiring, but never automatically certifies the packet path.
"""
import json
from pathlib import Path
import shutil
import subprocess
import time
from ffn_copper_vif import validate_profile


def main():
    source=Path(__file__).resolve().parent
    extension=Path('/opt/ffn-platforms/pa5200-management')
    dp=Path('/opt/ffn-cproot-owrt/opt/dproot')
    if json.loads((extension/'extension.json').read_text()).get('id')!='pa5200':
        raise RuntimeError('selected PA5200 platform required')
    ssh=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',
         '-o','UserKnownHostsFile=/etc/ffn-ngfw/plane_boot_known_hosts',
         '-o','ProxyCommand=ssh -F /etc/ffn-ngfw/ssh-cp.conf -W %h:%p ffn-cp','root@127.1.2.2']
    status=subprocess.run(ssh+['systemctl is-active ffn-vif.service'],capture_output=True,text=True,timeout=20)
    if status.returncode!=3 or status.stdout.strip()!='inactive':
        raise RuntimeError('VIF transport must be stopped before installing')
    saved=json.loads((dp/'etc/ffn/vifs.json').read_text())
    if saved['vifs']:raise RuntimeError('installation requires empty VIF assignments')
    files={name:dp/'usr/local/sbin'/name for name in ('ffn_vif_runtime.py','ffn_copper_vif.py','ffn_dp_packet_transport.py','ffn_inspection.py')}
    files.update({name:extension/name for name in ('vif_backend.py','vif-link-poll.py')})
    files.update({name:Path('/etc/systemd/system')/name for name in ('ffn-vif-links.service','ffn-vif-links.timer')})
    writes={target:(source/name).read_bytes() for name,target in files.items()}
    for name in files:
        if name.endswith('.py'):compile((source/name).read_text(),name,'exec')
    profile_path=dp/'etc/ffn/vif-copper.json'
    if profile_path.exists():validate_profile(json.loads(profile_path.read_text()))
    else:
        observed=subprocess.run(['/usr/local/sbin/ffn-faceplate','status'],capture_output=True,text=True,check=True,timeout=10)
        ports={}
        for row in json.loads(observed.stdout)['ports']:
            if row.get('port') in range(1,5) and row.get('phy_mapping_verified') is True:
                key=str(row['port'])
                if key in ports:raise ValueError('duplicate observed copper mapping')
                ports[key]={'phy':row['phy_address'],'bcm_port':row['bcm_port'],'packet_path_verified':False}
        profile={'version':1,'ports':ports};validate_profile(profile)
        writes[profile_path]=(json.dumps(profile,indent=2)+'\n').encode()
    ui_path=extension/'static/ui.js';ui=ui_path.read_text()
    start=ui.index('/* FFN PA5200 VIF UI v1:');end=ui.index('\n})();',start)+len('\n})();')
    writes[ui_path]=(ui[:start]+(source/'vif-ui.js').read_text().rstrip()+ui[end:]).encode()
    # Install the CP dependency before exposing the new MP observation path.
    controller=(source/'ffn_copper_forwarding.py').read_text()
    compile(controller,'ffn_copper_forwarding.py','exec')
    cp_install='''import fcntl,shutil,time
from pathlib import Path
source=SOURCE
path=Path('/usr/local/sbin/ffn_copper_forwarding.py')
compile(source,str(path),'exec')
with open('/run/ffn-copper-forwarding.lock','a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    backup=Path('/var/backups/ffn/copper-controller-'+str(time.time_ns()))
    if path.exists():
        backup.mkdir(parents=True);shutil.copy2(path,backup/path.name)
    temporary=path.with_suffix('.new');temporary.write_text(source)
    temporary.chmod(0o755);temporary.replace(path)
'''.replace('SOURCE',repr(controller))
    subprocess.run(['ssh','-F','/etc/ffn-ngfw/ssh-cp.conf','-o','BatchMode=yes',
        '-o','ConnectTimeout=5','ffn-cp','python3 -'],input=cp_install,text=True,
        check=True,timeout=20)
    backup=Path('/var/backups/ffn/copper-vif-'+str(time.time_ns()))
    manifest=[]
    for path in writes:
        previous=backup/str(path).lstrip('/')
        if path.exists():
            previous.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,previous)
        manifest.append({'path':str(path),'existed':path.exists()})
    backup.mkdir(parents=True,exist_ok=True)
    (backup/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    for path,data in writes.items():
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_name(path.name+'.copper-vif-new')
        temporary.write_bytes(data);temporary.chmod(0o644);temporary.replace(path)
    subprocess.run(['systemctl','daemon-reload'],check=True,timeout=20)
    subprocess.run(['systemctl','enable','--now','ffn-vif-links.timer'],check=True,timeout=20)
    print(json.dumps({'installed':True,'backup':str(backup),'vif_revision':saved['revision'],
                      'forwarding_started':False,'reboot_required':False}))


if __name__=='__main__':main()
