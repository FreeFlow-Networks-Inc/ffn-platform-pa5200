#!/usr/bin/env python3
"""Install staged VIF files on the selected PA5200 MP; do not start forwarding.

Stage this script, vif_api.py, vif_backend.py, vif-ui.js, ffn_vif.py,
ffn_vif_runtime.py, ffn_copper_vif.py, ffn_dp_packet_transport.py and
ffn-vif.service together. Run as root on the MP. Install the MP link timer
with install-copper-vif.py before commissioning copper packet paths.
Preserves other installed platform functions and creates timestamped backups.
"""
import json
from pathlib import Path
import py_compile
import shutil
import subprocess
import time

source=Path(__file__).resolve().parent
extension=Path('/opt/ffn-platforms/pa5200-management')
dp=Path('/opt/ffn-cproot-owrt/opt/dproot')
config=Path('/etc/ffn/planes/mp.json')
manifest=json.loads((extension/'extension.json').read_text())
assert manifest['id']=='pa5200' and dp.is_dir()
for name in ('vif_api.py','vif_backend.py','ffn_vif.py','ffn_vif_runtime.py','ffn_copper_vif.py','ffn_dp_packet_transport.py'):
    py_compile.compile(str(source/name),doraise=True)
backup=Path('/var/backups/ffn/vifs-'+str(time.time_ns()))
def write(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        saved=backup/str(path).lstrip('/');saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,saved)
    temp=path.with_name(path.name+'.vif-new');temp.write_bytes(data);temp.replace(path)
for name in ('vif_api.py','vif_backend.py'):
    write(extension/name,(source/name).read_bytes())
for name in ('ffn_vif.py','ffn_vif_runtime.py','ffn_copper_vif.py','ffn_dp_packet_transport.py'):
    write(dp/'usr/local/sbin'/name,(source/name).read_bytes())
write(dp/'etc/systemd/system/ffn-vif.service',(source/'ffn-vif.service').read_bytes())
network_path=dp/'usr/local/sbin/ffn_network.py'
network_code=network_path.read_text()
if 'from ffn_vif import protect_network' not in network_code:
    needle="    new['revision'] += 1\n    validate(new)\n"
    assert network_code.count(needle)==1
    network_code=network_code.replace(needle,needle+"    if STATE.with_name('vifs.json').exists():\n        from ffn_vif import protect_network\n        protect_network(new)\n")
    needle="        elif args.action == 'stop':\n"
    assert network_code.count(needle)==1
    network_code=network_code.replace(needle,needle+"            if STATE.with_name('vifs.json').exists() and json.loads(STATE.with_name('vifs.json').read_text())['vifs']:\n                raise RuntimeError('remove VIF assignments before stopping their network namespace')\n")
    compile(network_code,str(network_path),'exec');write(network_path,network_code.encode())
control=(extension/'control.py').read_text()
if "'pa5200_vif_api'" not in control:
    needle="    api = APIRouter(prefix=prefix, tags=['PA-5220 controls'])\n"
    assert control.count(needle)==1
    block="""    import importlib.util
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('pa5200_vif_api',Path(__file__).with_name('vif_api.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    api.include_router(module.router(current_user,require_admin,record_audit))
"""
    control=control.replace(needle,needle+block)
    compile(control,str(extension/'control.py'),'exec')
    write(extension/'control.py',control.encode())
ui=(extension/'static/ui.js').read_text();marker='/* FFN PA5200 VIF UI v1:'
ui=ui.split(marker)[0].rstrip()+'\n'+(source/'vif-ui.js').read_text()
write(extension/'static/ui.js',ui.encode())
manifest['pages']=[p for p in manifest['pages'] if p['id']!='vifs']+[{'id':'vifs','label':'Virtual Interfaces','tab':'network','after':'interfaces'}]
write(extension/'extension.json',(json.dumps(manifest,indent=2)+'\n').encode())
cfg=json.loads(config.read_text())
cfg['commands']['vifs']={op:['/usr/bin/python3',str(extension/'vif_backend.py'),op] for op in ('status','validate','apply')}
write(config,(json.dumps(cfg,indent=2)+'\n').encode())
print(json.dumps({'installed':True,'backup':str(backup),'forwarding_started':False,
                  'reload_required':['DP systemctl daemon-reload','MP ffn-plane@mp.service','MP ffn-manager-v2.service']}))
