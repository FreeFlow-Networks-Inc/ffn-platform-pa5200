#!/usr/bin/env python3
"""Install the copper identification resource without restarting BCM or PHYs.

Stage ffn_copper_identify.py, copper_identify_backend.py,
copper-identify-cli.py and copper-identify-ui.js alongside this script.
Adds a command to the existing MP configuration; preserves other resources.
"""
import base64
import json
from pathlib import Path
import shutil
import subprocess
import time


def main():
    source=Path(__file__).resolve().parent
    extension=Path('/opt/ffn-platforms/pa5200-management')
    if json.loads((extension/'extension.json').read_text()).get('id')!='pa5200':
        raise RuntimeError('selected PA5200 platform required')
    config=Path('/etc/ffn/planes/mp.json');original=config.read_bytes();cfg=json.loads(original)
    ui_path=extension/'static/ui.js';original_ui=ui_path.read_bytes();ui=original_ui.decode()
    api_path=extension/'control.py';original_api=api_path.read_bytes();api=original_api.decode()
    if "('copper-identify','status')" not in api:
        anchor='COMMANDS = {\n'
        if api.count(anchor)!=1:raise RuntimeError('installed hardware API command table differs')
        api=api.replace(anchor,anchor+"    ('copper-identify','status'): ('/usr/local/sbin/ffn-copper-identify','status'),\n    ('copper-identify','set'): ('/usr/local/sbin/ffn-copper-identify','apply'),\n")
        anchor="        if (resource, action) not in {"
        if api.count(anchor)!=1:raise RuntimeError('installed hardware API operation table differs')
        api=api.replace(anchor,anchor+"('copper-identify','set'), ")
        anchor="        allowed = {'phy':"
        if api.count(anchor)!=1:raise RuntimeError('installed hardware API field table differs')
        api=api.replace(anchor,"        allowed = {'copper-identify': {'revision','operation','port','token'}, 'phy':")
    compile(api,str(api_path),'exec')
    hook='    if(writable && window.ffnCopperIdentify)await window.ffnCopperIdentify.render(root,()=>faceplate(parent));\n'
    if hook not in ui:
        anchor="    const writable=['admin','superuser'].includes(user.role) && !data.saved?.pending;\n"
        if ui.count(anchor)!=1:raise RuntimeError('installed Faceplate Ports hook differs; review before installing')
        ui=ui.replace(anchor,anchor+hook)
    marker='/* FFN copper identification UI v1:'
    if marker in ui:
        start=ui.index(marker);end=ui.index('\n})();',start)+len('\n})();')
        ui=ui[:start]+ui[end:]
    ui=ui.rstrip()+'\n'+(source/'copper-identify-ui.js').read_text()
    for name in ('ffn_copper_identify.py','copper_identify_backend.py','copper-identify-cli.py'):
        compile((source/name).read_text(),name,'exec')
    commands={op:['/usr/bin/python3',str(extension/'copper_identify_backend.py'),op] for op in ('status','validate','apply')}
    if cfg.get('commands',{}).get('copper-identify',commands)!=commands:
        raise RuntimeError('another copper identification controller is already selected')
    cfg.setdefault('commands',{})['copper-identify']=commands
    backup=Path('/var/backups/ffn/copper-identify-'+str(time.time_ns()))
    payload=base64.b64encode((source/'ffn_copper_identify.py').read_bytes()).decode()
    # The CP runs only this fixed installer; no caller-provided shell operation.
    remote="""import base64,shutil,time
from pathlib import Path
p=Path('/usr/local/sbin/ffn_copper_identify.py')
data=base64.b64decode(%r)
compile(data,str(p),'exec')
if p.exists():
 b=Path('/var/backups/ffn/copper-identify-'+str(time.time_ns()));b.mkdir(parents=True);shutil.copy2(p,b/p.name)
t=p.with_name(p.name+'.new');t.write_bytes(data);t.chmod(0o644);t.replace(p)
"""%payload
    subprocess.run(['ssh','-F','/etc/ffn-ngfw/ssh-cp.conf','-o','BatchMode=yes','-o','ConnectTimeout=5',
                    'ffn-cp','python3 -'],input=remote,text=True,check=True,timeout=20)
    if config.read_bytes()!=original or ui_path.read_bytes()!=original_ui or api_path.read_bytes()!=original_api:
        raise RuntimeError('MP configuration/UI changed concurrently; rerun installer against fresh files')
    writes={extension/'copper_identify_backend.py':(source/'copper_identify_backend.py').read_bytes(),
            Path('/usr/local/sbin/ffn-copper-identify'):(source/'copper-identify-cli.py').read_bytes(),
            ui_path:ui.encode(),api_path:api.encode(),config:(json.dumps(cfg,indent=2)+'\n').encode()}
    for path,data in writes.items():
        if path.exists():
            saved=backup/str(path).lstrip('/');saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,saved)
        temp=path.with_name(path.name+'.identify-new');temp.write_bytes(data)
        temp.chmod(0o755 if path.name=='ffn-copper-identify' else 0o600 if path==config else 0o644)
        temp.replace(path)
    subprocess.run(['systemctl','restart','ffn-plane@mp.service'],check=True,timeout=30)
    if api_path.read_bytes()!=original_api:
        subprocess.run(['systemctl','restart','ffn-manager-v2.service'],check=True,timeout=110)
    print(json.dumps({'installed':True,'backup':str(backup),'reboot_required':False,
                      'physical_mapping_changed':False,'phy_registers_changed':False}))


if __name__=='__main__':main()
