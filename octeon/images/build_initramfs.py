#!/usr/bin/env python3
"""Create a credential-free Debian RAM bootstrap from source-built .deb inputs."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packages'))
from build_transport import audit_deb, static_mips
from build_images import audit_initramfs


def entry(name, data=b'', mode=0o100644, inode=1, epoch=0):
    name = name.encode() + b'\0'
    fields = [inode, mode, 0, 0, 1, epoch, len(data), 0, 0, 0, 0, len(name), 0]
    head = b'070701' + ''.join('%08x' % n for n in fields).encode() + name
    return head + b'\0' * (-len(head) % 4) + data + b'\0' * (-len(data) % 4)


def build(role, busybox, transport, base_files, output, epoch):
    if role not in ('cp', 'dp') or epoch < 0:
        raise ValueError('Invalid role or source timestamp')
    if output.exists():
        raise ValueError('Output already exists')
    audit_deb(transport)
    for package, name in [(busybox, 'busybox-static'), (base_files, 'base-files')]:
        fields = subprocess.check_output(['dpkg-deb', '-f', str(package), 'Package', 'Architecture'], text=True)
        if 'Package: '+name not in fields or 'Architecture: mips64' not in fields:
            raise ValueError('Expected native Debian MIPS64 '+name)
    with tempfile.TemporaryDirectory(prefix='ffn-initramfs-') as tmp:
        root = Path(tmp)
        for package in (busybox, transport, base_files):
            subprocess.run(['dpkg-deb', '-x', str(package), str(root)], check=True)
        files = {}
        for path in ('bin','sbin','etc','usr','usr/lib','dev','proc','sys','run','newroot'):
            files[path] = (b'', 0o040755)
        shell = next((root/p for p in ('bin/busybox','usr/bin/busybox') if (root/p).is_file()), None)
        if not shell:
            raise ValueError('Missing Debian busybox')
        static_mips(shell.read_bytes())
        files['bin/busybox'] = (shell.read_bytes(), 0o100755)
        files['bin/sh'] = (b'busybox', 0o120777)
        names = ['ffn_nfsmount','ffn-systemd-handoff'] + (['ffn_pcnetd'] if role=='cp' else ['ffn_dpagent2','ffn_dpnetd'])
        for name in names:
            files['sbin/'+name] = ((root/'usr/libexec/ffn'/name).read_bytes(), 0o100755)
        files['usr/lib/os-release'] = ((root/'usr/lib/os-release').read_bytes(), 0o100644)
        files['etc/os-release'] = (b'../usr/lib/os-release', 0o120777)
        files['etc/ffn-image-role'] = ((role+'\n').encode(), 0o100644)
        files['init'] = ((Path(__file__).parent/'boot/init').read_bytes().replace(b'\r\n',b'\n'), 0o100755)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('xb') as stream:
            for i, (name, (data, mode)) in enumerate(sorted(files.items()), 1):
                stream.write(entry(name, data, mode, i, epoch))
            stream.write(entry('TRAILER!!!', epoch=epoch))
        audit_initramfs(output)
    digest = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
    report = {'role':role, 'source_date_epoch':epoch, 'sha256':digest(output),
              'inputs':{str(p):digest(p) for p in (busybox,transport,base_files)},
              'hardware_boot_verified':False}
    output.with_suffix('.json').write_text(json.dumps(report,indent=2)+'\n')
    return report


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--role',choices=('cp','dp'),required=True)
    for name in ('busybox','transport','base-files','out'):
        p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--source-date-epoch',type=int,required=True)
    a=p.parse_args()
    print(json.dumps(build(a.role,a.busybox,a.transport,a.base_files,a.out,a.source_date_epoch),indent=2))
