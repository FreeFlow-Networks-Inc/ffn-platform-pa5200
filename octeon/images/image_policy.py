"""PA-5200 release input checks. Never execute target files or source os-release."""
import json
from pathlib import Path, PurePosixPath
import re
import shlex


POLICY = json.loads(Path(__file__).with_name('os-policy.json').read_text())
FOREIGN_MARKERS = ('etc/openwrt_release', 'etc/openwrt_version', 'etc/centos-release',
                   'etc/redhat-release', 'etc/opkg', 'usr/lib/opkg', 'var/lib/opkg',
                   'var/lib/rpm', 'usr/lib/sysimage/rpm', 'opt/ffn-compat', 'opt/dpfs')


def os_release(text):
    values = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or not re.fullmatch('[A-Z_0-9]+', key):
            raise ValueError('Invalid os-release record')
        fields = shlex.split(value, comments=False)
        if len(fields) > 1 or key in values:
            raise ValueError('Ambiguous os-release record')
        values[key] = fields[0] if fields else ''
    return values


def root_path(root, name):
    """Resolve absolute rootfs symlinks inside the image, never on the host."""
    root = Path(root).resolve()
    pending = list(PurePosixPath(name).parts)
    parts, links = [], 0
    while pending:
        part = pending.pop(0)
        if part in ('/', '.', ''): continue
        if part == '..':
            if not parts: raise ValueError('Image path escapes root')
            parts.pop()
            continue
        candidate = root.joinpath(*parts, part)
        if candidate.is_symlink():
            links += 1
            if links > 40: raise ValueError('Image symlink cycle')
            target = candidate.readlink()
            if target.is_absolute(): parts = []
            pending = list(target.parts) + pending
        else:
            parts.append(part)
    return root.joinpath(*parts)


def distribution(root, expected):
    records = []
    for name in ('etc/os-release', 'usr/lib/os-release'):
        p = root_path(root, name)
        if p.is_file(): records.append(os_release(p.read_text()))
    if not records or any(x.get('ID') != expected for x in records):
        raise ValueError('Expected ' + expected + ' os-release; mixed/unknown rootfs rejected')
    for name in FOREIGN_MARKERS:
        p = root / name
        if p.exists() or p.is_symlink():
            raise ValueError('Foreign distribution or compatibility root: ' + name)
    return records[0]


def debian_root(root, role=None):
    info = distribution(root, 'debian')
    status = root_path(root, 'var/lib/dpkg/status')
    if not status.is_file(): raise ValueError('Debian dpkg package database required')
    installed = {}
    for paragraph in re.split(r'\n\s*\n', status.read_text()):
        fields = dict(line.split(': ', 1) for line in paragraph.splitlines() if ': ' in line and not line.startswith(' '))
        if fields.get('Status') != 'install ok installed': continue
        name, arch = fields.get('Package'), fields.get('Architecture')
        if arch not in ('all', 'mips64'):
            raise ValueError('Non-MIPS64 big-endian Debian package: ' + str(name) + '/' + str(arch))
        if not name or not fields.get('Version'): raise ValueError('Incomplete dpkg package record')
        installed[name] = fields
    if not {'base-files', 'libc6', 'systemd', 'python3'} <= set(installed):
        raise ValueError('Debian base-files, glibc, systemd and Python packages required')
    if role is not None:
        if role not in ('cp', 'dp'):
            raise ValueError('Unknown OCTEON plane role')
        requirements = json.loads((Path(__file__).parent.parent / 'packages/runtime-requirements.json').read_text())
        missing = sorted(set(requirements['common'] + requirements[role]) - set(installed))
        if missing:
            raise ValueError('Missing configured ' + role + ' runtime packages: ' + ', '.join(missing))
    owned = set()
    infodir = root_path(root, 'var/lib/dpkg/info')
    for p in infodir.glob('*.list'):
        package = p.name[:-5].split(':', 1)[0]
        if package not in installed or p.is_symlink(): continue
        for name in p.read_text().splitlines():
            owned.add(root_path(root, name))
    for name in ('usr/lib/systemd/systemd', 'usr/bin/python3'):
        if root_path(root, name) not in owned:
            raise ValueError('Required runtime is not owned by a Debian package: ' + name)
    for directory in ('lib', 'lib64', 'usr/lib'):
        p = root_path(root, directory)
        if any(p.glob('ld-musl*')):
            raise ValueError('OpenWrt/musl loader is not a Debian glibc plane input')
    return {'distribution': 'debian', 'suite': info.get('VERSION_CODENAME', ''),
            'architecture': 'mips64', 'libc': 'glibc', 'package_count': len(installed)}


def kernel(tree):
    text = (tree / 'Makefile').read_text()
    version = []
    for key in ('VERSION', 'PATCHLEVEL', 'SUBLEVEL'):
        match = re.search(r'^' + key + r'\s*=\s*(\d+)\s*$', text, re.M)
        if not match: raise ValueError('Kernel source version cannot be established')
        version.append(int(match.group(1)))
    if version[:2] < POLICY['minimum_octeon_kernel']:
        raise ValueError('Legacy OCTEON kernel rejected: ' + '.'.join(map(str, version)))
    return '.'.join(map(str, version))


def compiler(target, version):
    text = (target + ' ' + version).lower()
    if not target.startswith('mips64') or any(x in text for x in ('openwrt', 'musl', 'mips64el')):
        raise ValueError('Debian plane builds require a MIPS64 big-endian GNU compiler')


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Audit PA-5200 Debian build inputs without executing target files')
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--rootfs', type=Path)
    inputs.add_argument('--kernel', type=Path)
    parser.add_argument('--role', choices=('cp', 'dp'), help='Also require the plane runtime package set')
    args = parser.parse_args()
    try:
        if args.role and not args.rootfs:
            raise ValueError('--role requires --rootfs')
        result = debian_root(args.rootfs, args.role) if args.rootfs else {'kernel_source_version': kernel(args.kernel)}
    except (ValueError, OSError) as exc:
        print(json.dumps({'accepted': False, 'error': str(exc)}))
        return 1
    print(json.dumps({'accepted': True, **result}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
