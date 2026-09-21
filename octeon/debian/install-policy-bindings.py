#!/usr/bin/env python3
"""Install the selected PA5200 policy binding adapter on its dataplane.

Discovers live aggregate/VLAN owners; does not apply policies or change links.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import time


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library',type=Path,default=Path('/usr/local/lib/ffn'))
    args=parser.parse_args()
    if os.geteuid()!=0 or not args.library.is_absolute():raise SystemExit('Root and an absolute library directory are required')
    source=Path(__file__).with_name('ffn_platform_policy_bindings.py')
    data=source.read_bytes();compile(data,str(source),'exec')
    selection=Path('/etc/ffn/policy-bindings.json')
    if selection.exists() and json.loads(selection.read_text())!={'provider':'platform'}:
        raise SystemExit('Another policy binding provider is selected')
    writes={args.library/source.name:data,selection:b'{"provider":"platform"}\n'}
    backup=Path('/var/backups/ffn/policy-bindings-'+str(time.time_ns()))
    for target,data in writes.items():
        if target.exists():
            saved=backup/target.relative_to('/');saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,saved)
        target.parent.mkdir(parents=True,exist_ok=True)
        temp=target.with_name(target.name+'.new');temp.write_bytes(data);temp.chmod(0o644);temp.replace(target)
    print(json.dumps({'installed':True,'backup':str(backup),'configuration_applied':False,'reboot_required':False}))


if __name__=='__main__':main()
