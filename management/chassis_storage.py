#!/usr/bin/env python3
"""Read disk I/O without probing or waking drives; map only verified bay paths."""
import json
from pathlib import Path
import time

BAYS=('SYS 1','SYS 2','LOG 1','LOG 2')
# PA-5200 raid/__init__.py maps SCSI hosts 0,1,3,4 to these bays.
# Correlated with the PA-5220 AHCI controller and live persistent disk paths.
DEFAULT_BAYS={name:'pci-0000:00:1f.2-ata-%d'%port for name,port in zip(BAYS,(1,2,4,5))}


def sample(sysfs=Path('/sys/class/block'), paths=Path('/dev/disk/by-path'), mapping=Path('/etc/ffn-ngfw/drive-bays.json')):
    configured=json.loads(mapping.read_text()) if mapping.exists() else DEFAULT_BAYS
    if (not isinstance(configured,dict) or set(configured)-set(BAYS) or
        any(not isinstance(p,str) or '/' in p or '-part' in p for p in configured.values()) or
        len(set(configured.values()))!=len(configured)):raise ValueError('Invalid drive bay mapping')
    disks=[]
    available_paths={p.name:p.resolve().name for p in paths.glob('*') if '-part' not in p.name and p.exists()}
    for node in sysfs.iterdir():
        if (node/'partition').exists() or not (node/'device').exists():continue
        try:
            values=[int(v) for v in (node/'stat').read_text().split()]
            aliases=sorted(name for name,device in available_paths.items() if device==node.name)
            disks.append(dict(name=node.name,paths=aliases,model=(node/'device/model').read_text().strip(),
                state=(node/'device/state').read_text().strip(),read_bytes=values[2]*512,write_bytes=values[6]*512,
                io_in_progress=values[8]))
        except (OSError,ValueError,IndexError):continue
    bays=[]
    for bay in BAYS:
        path=configured.get(bay)
        disk=next((d for d in disks if path in d['paths']),None) if path else None
        bays.append(dict(name=bay,mapped=bool(path),disk=disk,state='unmapped' if not path else
            'present' if disk else 'unavailable' if path in available_paths else 'absent'))
    return dict(observed_at=time.time(),sample_monotonic=time.monotonic(),bays=bays,disks=disks,
        note='PA-5220 rear bays mapped by verified AHCI paths; disk enumeration order is not bay order.')


if __name__=='__main__':print(json.dumps(sample()))
