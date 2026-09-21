#!/usr/bin/env python3
"""Build an isolated DP kernel candidate; never select or boot the result."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess as S


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--tree',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--cross',required=True);p.add_argument('--localversion',required=True)
    p.add_argument('--jobs',type=int,default=4);a=p.parse_args()
    source=a.tree.resolve();out=a.out.resolve()
    if out.exists() or source==out or source in out.parents or out in source.parents:raise SystemExit('Use a new, separate output directory')
    if not 1<=a.jobs<=32 or not a.localversion.startswith('-'):raise SystemExit('Invalid build settings')
    inputs={name:sha(source/name) for name in ('.config','Module.symvers','include/generated/autoconf.h')}
    out.mkdir(parents=True);tree=out/'linux-dp'
    S.run(['cp','-a','--reflink=auto',str(source),str(tree)],check=True)
    symbols=['NF_CONNTRACK_EVENTS','NF_CONNTRACK_LABELS','NF_CT_NETLINK','NF_CONNTRACK_MARK',
             'NFT_CT','NFT_LOG','NF_LOG_SYSLOG','NFT_LIMIT','NFT_REJECT','NFT_REJECT_INET']
    S.run([str(tree/'scripts/config'),'--file',str(tree/'.config'),
           *[word for symbol in symbols for word in ('--enable',symbol)],
           '--set-str','LOCALVERSION',a.localversion,'--disable','LOCALVERSION_AUTO'],check=True)
    for symbol in ('NFT_NUMGEN','NFT_HASH','NET_SCH_HTB','NET_SCH_FQ_CODEL','NET_CLS_FW'):
        S.run([str(tree/'scripts/config'),'--file',str(tree/'.config'),'--module',symbol],check=True)
    make=['make','-C',str(tree),'ARCH=mips','CROSS_COMPILE='+a.cross]
    S.run(make+['olddefconfig'],check=True)
    config=(tree/'.config').read_text()
    for symbol in symbols:
        if 'CONFIG_'+symbol+'=y\n' not in config:raise SystemExit('Required feature unavailable: '+symbol)
    S.run(make+['-j'+str(a.jobs),'vmlinux','modules'],check=True)
    image=out/'vmlinux-dp-security'
    S.run([a.cross+'strip','-o',str(image),str(tree/'vmlinux')],check=True)
    S.run(make+['INSTALL_MOD_PATH='+str(out/'modules'),'modules_install'],check=True)
    if inputs!={name:sha(source/name) for name in inputs}:raise SystemExit('Original kernel inputs changed during build')
    header=image.read_bytes()[:20]
    if header[:6]!=b'\x7fELF\x02\x02' or int.from_bytes(header[18:20],'big')!=8:raise SystemExit('Incorrect image ABI')
    manifest=dict(source=str(source),inputs=inputs,release=(tree/'include/config/kernel.release').read_text().strip(),
                  config_sha256=sha(tree/'.config'),image_sha256=sha(image),
                  compiler=S.check_output([a.cross+'gcc','--version'],text=True).splitlines()[0],
                  hardware_boot_verified=False,selected_for_boot=False)
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps(manifest),flush=True)


if __name__=='__main__':main()
