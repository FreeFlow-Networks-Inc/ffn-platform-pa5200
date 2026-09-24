#!/usr/bin/env python3
"""Install platform-owned MP BCM client modules; does not restart services."""
import argparse
import json
import os
from pathlib import Path
import shutil
import time

SOURCE = Path(__file__).resolve().parents[1]
FILES = {'octeon/bcmagent/ffn_bcm_client.py': 'ffn_bcm_client.py',
         'ffn_bcmports.py': 'ffn_bcmports.py', 'ffn_ifroles.py': 'ffn_ifroles.py'}

def install(target):
    if json.loads((target/'extension.json').read_text()).get('id') != 'pa5200':
        raise ValueError('Selected PA5200 management extension required')
    backup = target/'backups'/('bcm-client-'+str(time.time_ns()))
    for source, name in FILES.items():
        data = (SOURCE/source).read_text()
        compile(data, source, 'exec')
    for source, name in FILES.items():
        destination = target/name
        if destination.exists():
            backup.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, backup/name)
        temporary = target/(name+'.new')
        shutil.copyfile(SOURCE/source, temporary)
        temporary.chmod(0o644)
        temporary.replace(destination)
    return {'installed': list(FILES.values()), 'backup': str(backup),
            'restart_required': ['ffn-managementd.service']}

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, default=Path('/opt/ffn-platforms/pa5200-management'))
    args = parser.parse_args()
    if os.name != 'posix' or os.geteuid() != 0:
        parser.error('Run on the Linux MP as root')
    print(json.dumps(install(args.target)))
