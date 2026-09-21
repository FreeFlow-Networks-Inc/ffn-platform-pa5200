#!/usr/bin/env python3
"""Rebuild externally supplied drivers whose source artifacts match live hashes."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess as S


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--candidate',type=Path,required=True);p.add_argument('--inventory',type=Path,required=True)
    p.add_argument('--cross',required=True);a=p.parse_args()
    out=a.candidate.resolve();tree=out/'linux-dp';manifest=json.loads((out/'manifest.json').read_text())
    release=(tree/'include/config/kernel.release').read_text().strip()
    if release!=manifest['release'] or sha(tree/'.config')!=manifest['config_sha256']:raise SystemExit('Candidate configuration changed')
    inventory=json.loads(a.inventory.read_text());work=out/'external-drivers'
    if work.exists():raise SystemExit('External driver build already exists')
    for name,row in inventory.items():
        source=Path(row['source']).resolve()
        if not name.replace('_','').isalnum() or source==out or out in source.parents:raise SystemExit('Invalid driver source')
        if sha(source/(name+'.ko'))!=row['live_sha256']:raise SystemExit('Source artifact does not match live driver: '+name)
    work.mkdir();artifacts={}
    for name,row in inventory.items():
        source=Path(row['source']).resolve();target=work/name
        S.run(['cp','-a','--reflink=auto',str(source),str(target)],check=True)
        S.run(['make','-C',str(tree),'ARCH=mips','CROSS_COMPILE='+a.cross,'M='+str(target),'-j4','modules'],check=True)
        built=target/(name+'.ko')
        vermagic=S.check_output(['modinfo','-F','vermagic',str(built)],text=True).strip()
        if vermagic.split()[0]!=release:raise SystemExit('Driver release mismatch')
        data=built.read_bytes()
        if data[:6]!=b'\x7fELF\x02\x02' or int.from_bytes(data[18:20],'big')!=8:raise SystemExit('Incorrect driver ABI')
        destination=out/'modules/lib/modules'/release/'extra'/built.name
        destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(built,destination)
        if sha(source/(name+'.ko'))!=row['live_sha256']:raise SystemExit('Original driver artifact changed')
        artifacts[name]=dict(row,sha256=sha(built),vermagic=vermagic,
                            dependencies=S.check_output(['modinfo','-F','depends',str(built)],text=True).strip())
    S.run(['depmod','-b',str(out/'modules'),release],check=True)
    (out/'external-drivers.json').write_text(json.dumps(artifacts,indent=2)+'\n')
    print(json.dumps({'release':release,'drivers':artifacts,'hardware_boot_verified':False}))


if __name__=='__main__':main()
