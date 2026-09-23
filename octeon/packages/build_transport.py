#!/usr/bin/env python3
"""Build an unactivated Debian MIPS64 transport package and corresponding source."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile


SOURCES = {
    'octeon/pcnet/ffn_pcnetd_oct.c': 'pcnet/ffn_pcnetd_oct.c',
    'octeon/pcnet/ffn_pcnet.h': 'pcnet/ffn_pcnet.h',
    'octeon/pcnet/Makefile': 'pcnet/Makefile',
    'octeon/dpnet2/ffn_dpnetd.c': 'dpnet/ffn_dpnetd.c',
    'octeon/dpnet2/ffn_dpnet_ring.h': 'dpnet/ffn_dpnet_ring.h',
    'octeon/dpnet2/Makefile': 'dpnet/Makefile',
    'octeon/dpagent/ffn_dpagent2.c': 'boot/ffn_dpagent2.c',
    'octeon/debian/ffn-systemd-handoff.c': 'boot/ffn-systemd-handoff.c',
    'octeon/images/boot/Makefile': 'boot/Makefile',
    'octeon/images/boot/ffn_nfsmount.c': 'boot/ffn_nfsmount.c',
    'LICENSE': 'LICENSE',
}
BINARIES = {'usr/libexec/ffn/' + name for name in
            ('ffn_pcnetd', 'ffn_dpnetd', 'ffn_dpagent2', 'ffn-systemd-handoff', 'ffn_nfsmount')}


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def static_mips(data):
    if len(data) < 64 or data[:6] != b'\x7fELF\x02\x02' or data[18:20] != b'\x00\x08':
        raise ValueError('Transport is not MIPS64 big-endian ELF')
    if int.from_bytes(data[16:18], 'big') not in (2, 3):
        raise ValueError('Transport must be an executable ELF')
    offset = int.from_bytes(data[32:40], 'big')
    size = int.from_bytes(data[54:56], 'big')
    count = int.from_bytes(data[56:58], 'big')
    if not count or size < 56 or offset < 64 or offset + size * count > len(data):
        raise ValueError('Invalid ELF program headers')
    types = {int.from_bytes(data[offset+i*size:offset+i*size+4], 'big') for i in range(count)}
    if 1 not in types:
        raise ValueError('Executable has no loadable segment')
    if 3 in types:
        raise ValueError('Transport must not depend on a network-backed ELF interpreter')


def audit_deb(path):
    control = subprocess.check_output(['dpkg-deb', '-f', str(path)], text=True)
    fields = dict(row.split(': ', 1) for row in control.splitlines() if ': ' in row and not row.startswith(' '))
    if fields.get('Package') != 'ffn-octeon-transport' or fields.get('Architecture') != 'mips64':
        raise ValueError('Unexpected transport package identity or architecture')
    if not fields.get('Built-Using'):
        raise ValueError('Static runtime source provenance missing')
    scripts = subprocess.check_output(['dpkg-deb', '--ctrl-tarfile', str(path)])
    import io
    with tarfile.open(fileobj=io.BytesIO(scripts)) as archive:
        if any(Path(m.name).name in ('preinst', 'postinst', 'prerm', 'postrm', 'triggers') for m in archive):
            raise ValueError('Transport package must not activate hardware during installation')
    found = {}
    process = subprocess.Popen(['dpkg-deb', '--fsys-tarfile', str(path)], stdout=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=process.stdout, mode='r|') as archive:
            for member in archive:
                name = member.name.removeprefix('./')
                if PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts:
                    raise ValueError('Unsafe package path')
                if member.mode & 0o6000 or member.isdev() or member.uid or member.gid:
                    raise ValueError('Privileged files or device nodes in package')
                if not (member.isfile() or member.isdir()):
                    raise ValueError('Unexpected link or special file in package')
                if name in BINARIES:
                    if name in found:
                        raise ValueError('Duplicate transport executable')
                    if not member.isfile() or member.mode != 0o755 or member.size > 32 * 1024**2:
                        raise ValueError('Invalid transport executable')
                    data = archive.extractfile(member).read()
                    static_mips(data)
                    found[name] = hashlib.sha256(data).hexdigest()
                elif not member.isdir() and not name.startswith('usr/share/doc/'):
                    raise ValueError('Unexpected package payload: ' + name)
    finally:
        process.stdout.close()
        status = process.wait()
    if status != 0:
        raise ValueError('Cannot inspect Debian package')
    if set(found) != BINARIES:
        raise ValueError('Both PCIe transports and boot helpers are required')
    return {'package': fields['Package'], 'version': fields['Version'],
            'architecture': fields['Architecture'], 'built_using': fields['Built-Using'],
            'executables': found}


def build(platform, out, version, epoch):
    if not re.fullmatch(r'[0-9][A-Za-z0-9+.~]*', version):
        raise ValueError('Use a native Debian version without a Debian revision or epoch')
    if epoch < 0:
        raise ValueError('Invalid source epoch')
    if not re.search(r'^ID="?debian"?$', Path('/etc/os-release').read_text(), re.M):
        raise ValueError('Run inside the Debian build environment')
    out.mkdir(parents=True, exist_ok=False)
    source = out / ('ffn-octeon-transport-' + version)
    source.mkdir()
    inputs = {}
    for origin, destination in SOURCES.items():
        src, dst = platform / origin, source / destination
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        inputs[origin] = sha(src)
    packaging = platform / 'octeon/packages/transport/debian'
    shutil.copytree(packaging, source / 'debian')
    for p in packaging.rglob('*'):
        if p.is_file():
            inputs[p.relative_to(platform).as_posix()] = sha(p)
    date = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000')
    (source / 'debian/changelog').write_text(
        'ffn-octeon-transport ('+version+') unstable; urgency=medium\n\n'
        '  * Source-built Debian OCTEON transport candidate; no activation.\n\n'
        ' -- FFN Build System <build@localhost>  '+date+'\n')
    (source / 'debian/rules').chmod(0o755)
    (source / 'source-inputs.json').write_text(json.dumps(inputs, sort_keys=True, indent=2)+'\n')
    env = dict(os.environ, SOURCE_DATE_EPOCH=str(epoch))
    with (out / 'build.log').open('w') as log:
        subprocess.run(['dpkg-buildpackage', '-a', 'mips64', '-us', '-uc'], cwd=source,
                       env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    packages = list(out.glob('*.deb'))
    if len(packages) != 1:
        raise ValueError('Expected one transport package')
    report = audit_deb(packages[0])
    report.update(schema=1, source_inputs=inputs, source_date_epoch=epoch,
                  hardware_qualified=False, published=False,
                  static_runtime_sources_required=report['built_using'])
    report['artifacts'] = {p.name: sha(p) for p in sorted(out.iterdir()) if p.is_file() and p.name != 'build.log'}
    (out / 'manifest.json').write_text(json.dumps(report, sort_keys=True, indent=2)+'\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--source-date-epoch', type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.platform.resolve(), args.out.resolve(), args.version, args.source_date_epoch), indent=2))
