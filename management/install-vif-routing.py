#!/usr/bin/env python3
"""Install staged VIF routing modules on the MP with the DP VIF service stopped.

Stage ffn_linux_network.py from the pinned core commit alongside ffn_network.py,
ffn_vif_runtime.py, ffn_copper_vif.py, ffn_dp_packet_transport.py, this script,
and ui.js. Saves originals before replacement;
does not change port assignments, route configuration, or start services.
"""
import json
from pathlib import Path
import shutil
import subprocess
import time

source=Path(__file__).resolve().parent
dp=Path('/opt/ffn-cproot-owrt/opt/dproot')
extension=Path('/opt/ffn-platforms/pa5200-management')
assert json.loads((extension/'extension.json').read_text())['id']=='pa5200'
vifs=json.loads((dp/'etc/ffn/vifs.json').read_text())
if vifs['vifs']:raise RuntimeError('installation requires empty VIF assignments')
status=subprocess.run(['python3','/tmp/ffn-dp-ssh.py','systemctl','is-active','ffn-vif.service'],capture_output=True,text=True)
if status.stdout.strip()!='inactive':raise RuntimeError('stop VIF transport before installing')
files={'ffn_linux_network.py':dp/'usr/local/lib/ffn/ffn_linux_network.py',
       'ffn_network.py':dp/'usr/local/sbin/ffn_network.py',
       'ffn_vif_runtime.py':dp/'usr/local/sbin/ffn_vif_runtime.py',
       'ffn_copper_vif.py':dp/'usr/local/sbin/ffn_copper_vif.py',
       'ffn_dp_packet_transport.py':dp/'usr/local/sbin/ffn_dp_packet_transport.py'}
for name in files:compile((source/name).read_text(),name,'exec')
core=(source/'ffn_linux_network.py').read_text()
assert 'def routing_interfaces(cfg):' in core and 'def attached_interfaces(attachment):' in core
ui=(extension/'static/ui.js').read_text()
ui=ui.replace('Forwarding uses the commissioning relay on ports 1, 3, 5 and 13; MTU 1500. Hardware flow offload is not active.',
              'The VIF packet transport is commissioned on ports 5 and 13 with MTU 1500. Check Virtual Interfaces for active assignments and Faceplate Ports for link state. Hardware flow offload is not active.')
start="      if (section === 'routing') {\n        element('p', 'Routes and policy rules can use enabled L3 VIF names"
staged=(source/'ui.js').read_text()
block=staged[staged.index(start):staged.index("      if (section === 'interfaces') {",staged.index(start))]
if 'Routes and policy rules can use enabled L3 VIF names' not in ui:
    needle="      if (!r.available) { element('p', r.error, box); continue; }\n"
    assert ui.count(needle)==1
    ui=ui.replace(needle,needle+block)
backup=Path('/var/backups/ffn/vif-routing-'+str(time.time_ns()))
def write(target,data):
    target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists():
        saved=backup/str(target).lstrip('/');saved.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(target,saved)
    temporary=target.with_name(target.name+'.routing-new')
    temporary.write_bytes(data);temporary.chmod(0o644);temporary.replace(target)
for name,target in files.items():write(target,(source/name).read_bytes())
write(extension/'static/ui.js',ui.encode())
print(json.dumps({'installed':True,'backup':str(backup),'vif_revision':vifs['revision'],'services_started':False}))
