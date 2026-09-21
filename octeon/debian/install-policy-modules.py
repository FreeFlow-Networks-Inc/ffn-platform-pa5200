#!/usr/bin/env python3
"""Install ABI-checked policy modules without changing traffic or boot images."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile

NAMES=('nft_numgen','nft_hash','sch_htb','sch_fq_codel','cls_fw')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive',type=Path)
    parser.add_argument('--persist',action='store_true',help='Load at boot after target packet validation')
    args=parser.parse_args()
    if os.geteuid()!=0:raise SystemExit('Root required')
    release=os.uname().release
    with tarfile.open(args.archive) as archive:
        members=archive.getmembers()
        expected={'manifest.json'}|{name+'.ko' for name in NAMES}
        if len(members)!=len(expected) or {m.name for m in members}!=expected or any(not m.isfile() for m in members):
            raise SystemExit('Unexpected archive members')
        manifest=json.load(archive.extractfile('manifest.json'))
        if manifest['release']!=release:raise SystemExit('Kernel release mismatch')
        config=gzip.decompress(Path('/proc/config.gz').read_bytes())
        if hashlib.sha256(config).hexdigest()!=manifest['inputs']['.config']:
            raise SystemExit('Running kernel configuration does not match the module build')
        binaries={name:archive.extractfile(name+'.ko').read() for name in NAMES}
    target=Path('/lib/modules')/release/'extra/ffn-policy'
    for name,data in binaries.items():
        wanted=manifest['modules'][name+'.ko']
        if hashlib.sha256(data).hexdigest()!=wanted['sha256'] or data[:6]!=b'\x7fELF\x02\x02' or int.from_bytes(data[18:20],'big')!=8:
            raise SystemExit('Module hash or MIPS64eb ABI mismatch')
        actual=data.split(b'vermagic=',1)[-1].split(b'\0',1)[0].decode('ascii',errors='replace').strip()
        if actual!=wanted['vermagic'] or wanted['vermagic'].split()[0]!=release:
            raise SystemExit('Module version evidence mismatch')
        path=target/(name+'.ko')
        if path.exists() and path.read_bytes()!=data:raise SystemExit('Existing module differs; explicit upgrade required')
    target.mkdir(parents=True,exist_ok=True)
    for name,data in binaries.items():
        path=target/(name+'.ko');path.write_bytes(data);path.chmod(0o644)
        if not (Path('/sys/module')/name).exists():
            subprocess.run(['insmod',str(path)],check=True,timeout=15)
    subprocess.run(['depmod','-a',release],check=True,timeout=15)
    (target/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    if args.persist:
        Path('/etc/modules-load.d/ffn-policy.conf').write_text('\n'.join(NAMES)+'\n')
    print(json.dumps({'release':release,'loaded':list(NAMES),'persistent':args.persist,'reboot_required':False}))


if __name__=='__main__':main()
