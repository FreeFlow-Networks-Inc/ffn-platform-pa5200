#!/usr/bin/env python3
"""Build additional modules against an existing commissioned kernel tree.

Never changes Kconfig or boot images. Modpost must resolve every dependency.
The output manifest binds artifacts to config, symbol versions and release.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

SOURCES={'nft_numgen':'net/netfilter/nft_numgen.c','nft_hash':'net/netfilter/nft_hash.c',
         'sch_htb':'net/sched/sch_htb.c','sch_fq_codel':'net/sched/sch_fq_codel.c',
         'cls_fw':'net/sched/cls_fw.c'}


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tree',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--cross',required=True)
    parser.add_argument('--release',required=True)
    args=parser.parse_args();tree=args.tree.resolve();out=args.out.resolve()
    if out==tree or tree in out.parents or out.exists():raise SystemExit('Use a new output directory outside the kernel tree')
    release=(tree/'include/config/kernel.release').read_text().strip()
    if release!=args.release:raise SystemExit('Kernel release does not match requested target')
    before={name:sha(tree/name) for name in ('.config','Module.symvers','include/generated/autoconf.h')}
    out.mkdir(parents=True)
    for name,source in SOURCES.items():shutil.copy2(tree/source,out/(name+'.c'))
    (out/'Makefile').write_text('obj-m := '+' '.join(name+'.o' for name in SOURCES)+'\n')
    command=['make','-C',str(tree),'ARCH=mips','CROSS_COMPILE='+args.cross,'M='+str(out),'-j4','modules']
    subprocess.run(command,check=True)
    if before!={name:sha(tree/name) for name in before}:raise SystemExit('Kernel build inputs changed; artifacts not qualified')
    artifacts={}
    for name in SOURCES:
        path=out/(name+'.ko');header=path.read_bytes()[:20]
        if header[:6]!=b'\x7fELF\x02\x02' or int.from_bytes(header[18:20],'big')!=8:raise SystemExit('Incorrect module ABI')
        vermagic=subprocess.check_output(['modinfo','-F','vermagic',str(path)],text=True).strip()
        if vermagic.split()[0]!=release:raise SystemExit('Module vermagic mismatch')
        artifacts[path.name]=dict(sha256=sha(path),vermagic=vermagic,
            source_sha256=sha(tree/SOURCES[name]),
            dependencies=subprocess.check_output(['modinfo','-F','depends',str(path)],text=True).strip().split(','))
    manifest=dict(release=release,inputs=before,modules=artifacts,
        compiler=subprocess.check_output([args.cross+'gcc','--version'],text=True).splitlines()[0])
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps(manifest))


if __name__=='__main__':main()
