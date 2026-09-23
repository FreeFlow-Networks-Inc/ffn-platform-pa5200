#!/usr/bin/env python3
"""Bootstrap a new Debian MIPS64 root from packages; never import a running root."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import image_policy
from build_images import audit_root


def build(role, repository, out):
    if role not in ('cp','dp') or os.geteuid() != 0:
        raise ValueError('Run as root on the isolated Linux builder; select cp or dp')
    repository = repository.resolve(strict=True)
    if not (repository/'dists/rebootstrap/Release').is_file() or any(c in str(repository) for c in ' \t\n[]'):
        raise ValueError('Expected the source-built rebootstrap package repository')
    requirements=json.loads((Path(__file__).resolve().parents[1]/'packages/runtime-requirements.json').read_text())
    packages=sorted(set(requirements['common']+requirements[role]+['ca-certificates','netbase','login','mount','util-linux']))
    out.mkdir(parents=True,exist_ok=False)
    root=out/'root'
    subprocess.run(['mmdebstrap','--architectures=mips64','--variant=apt','--format=directory',
                    '--include='+','.join(packages),'--aptopt=Acquire::Languages "none"',
                    '--hook-dir=/usr/share/mmdebstrap/hooks/file-mirror-automount',
                    '--setup-hook=mkdir -p "$1/usr/lib64"; test -e "$1/lib64" || ln -s usr/lib64 "$1/lib64"',
                    '',str(root),'deb [trusted=yes arch=mips64,all] file://'+str(repository)+' rebootstrap main',
                    'deb [arch=all] https://deb.debian.org/debian sid main',
                    'deb-src https://deb.debian.org/debian sid main'],check=True)
    # Only this newly bootstrapped tree is sanitized. Never accept a supplied root.
    if subprocess.check_output(['chroot',str(root),'dpkg','--audit'],text=True).strip():
        raise ValueError('The bootstrap contains unconfigured packages')
    inventory=subprocess.check_output(['chroot',str(root),'dpkg-query','-W',
            '-f=${Package}\t${Version}\t${Architecture}\t${source:Package}\t${source:Version}\n'],text=True)
    (out/'packages.tsv').write_text(inventory)
    for rel in ('var/log','tmp','run','var/cache/apt/archives','var/lib/apt/lists'):
        directory=root/rel
        if directory.is_symlink() or os.path.ismount(directory):
            raise ValueError('Unexpected seed mount or symlink: '+rel)
        if directory.is_dir():
            for child in directory.iterdir():
                if child.is_dir() and not child.is_symlink(): shutil.rmtree(child)
                else: child.unlink()
    for rel in ('etc/machine-id','var/lib/dbus/machine-id','var/lib/systemd/random-seed'):
        (root/rel).unlink(missing_ok=True)
    for path in (root/'etc/ssh').glob('ssh_host_*'):
        path.unlink()
    # An empty machine ID requests local initialization at first provisioned boot.
    (root/'etc/machine-id').touch()
    (root/'etc/hostname').write_text('ffn-'+role+'\n')
    # The local build repository does not exist on the appliance. MP updates are
    # signed complete images, never arbitrary apt downloads on a processor.
    for path in [root/'etc/apt/sources.list',*(root/'etc/apt/sources.list.d').glob('*')]:
        if path.is_file() or path.is_symlink(): path.unlink()
    image_policy.debian_root(root,role=role)
    audit_root(root)
    archive=out/(role+'-clean.tar.xz')
    subprocess.run(['tar','--numeric-owner','-C',str(root),'-cJf',str(archive),'.'],check=True,
                   env=dict(os.environ,XZ_OPT='-T4'))
    with archive.open('rb') as stream:
        digest=hashlib.file_digest(stream,'sha256').hexdigest()
    report={'role':role,'sha256':digest,
            'package_count':len(inventory.splitlines()),'hardware_boot_verified':False}
    (out/'manifest.json').write_text(json.dumps(report,indent=2)+'\n')
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--role',choices=('cp','dp'),required=True)
    p.add_argument('--repository',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();print(json.dumps(build(a.role,a.repository,a.out),indent=2))
