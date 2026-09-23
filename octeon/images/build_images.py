#!/usr/bin/env python3
"""Build CP/DP release candidates from clean, pinned inputs on a Linux builder.

No input may be an appliance root or recovered vendor sysroot. The protected
runner configuration identifies reviewed kernel source and rootfs/initramfs
seeds. Builds never contact an appliance or change any boot selection.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import tarfile
import tempfile
import image_policy


def run(args, **kw):
    return subprocess.run([str(x) for x in args], check=True, **kw)


def output(args):
    return subprocess.check_output([str(x) for x in args], text=True).strip()


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def revision(repo):
    if output(['git', '-C', repo, 'status', '--porcelain', '--untracked-files=no']):
        raise ValueError('Build source contains tracked modifications')
    return output(['git', '-C', repo, 'rev-parse', 'HEAD'])


def pinned_file(item):
    path = Path(item['path']).resolve(strict=True)
    if not re.fullmatch('[0-9a-f]{64}', item['sha256']) or sha(path) != item['sha256']:
        raise ValueError('Build input digest mismatch: ' + str(path))
    return path


def elf(path):
    with Path(path).open('rb') as f:
        header = f.read(20)
    if header[:6] != b'\x7fELF\x02\x02' or header[18:20] != b'\x00\x08':
        raise ValueError('Expected MIPS64 big-endian ELF: ' + str(path))


def private_key_material(data):
    # ssh-keygen/OpenSSL embed PEM delimiter strings as executable constants.
    # Require an actual encoded body, not a delimiter alone. Do not exempt ELF
    # files wholesale: a binary can still contain an embedded private key.
    return re.search(rb'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----\r?\n'
                     rb'(?:(?:Proc-Type|DEK-Info):[^\r\n]*\r?\n|\r?\n)*'
                     rb'[A-Za-z0-9+/=]{32,}\r?\n', data) is not None


def rootfs_filter(member, destination):
    # Debian alternatives and CA links are absolute inside the target root.
    # Rebase them before Python's safety filter so they never refer to the host.
    if (member.issym() or member.islnk()) and member.linkname.startswith('/'):
        if '..' in member.linkname.split('/'):
            raise ValueError('Absolute image link traverses outside root')
        member = copy.copy(member)
        target = member.linkname.lstrip('/')
        member.linkname = posixpath.relpath(target, posixpath.dirname(member.name) or '.') if member.issym() else target
    return tarfile.data_filter(member, destination)


def audit_initramfs(path):
    """Only a single plain newc archive is accepted; inspect without extracting."""
    distributions = []
    with path.open('rb') as f:
        while True:
            header = f.read(110)
            if len(header) != 110 or header[:6] != b'070701':
                raise ValueError('Clean uncompressed newc initramfs required')
            fields = [int(header[i:i+8], 16) for i in range(6, 110, 8)]
            mode, size, namesize = fields[1], fields[6], fields[11]
            if not 0 < namesize <= 4096 or size > 256 * 1024**2:
                raise ValueError('Invalid initramfs member bounds')
            raw_name = f.read(namesize)
            if len(raw_name) != namesize or raw_name[-1:] != b'\0':
                raise ValueError('Invalid initramfs name')
            name = raw_name[:-1].decode('utf8').removeprefix('./')
            f.read(-(110 + namesize) % 4)
            data = f.read(size)
            if len(data) != size:
                raise ValueError('Truncated initramfs')
            f.read(-size % 4)
            if name == 'TRAILER!!!':
                if size or any(f.read()):
                    raise ValueError('Concatenated initramfs content is not supported')
                if not distributions or any(x.get('ID') != 'debian' for x in distributions):
                    raise ValueError('Initramfs must identify a Debian userspace; foreign or unknown seed rejected')
                return
            if name.startswith('/') or '..' in name.split('/'):
                raise ValueError('Unsafe initramfs path')
            if mode & 0o170000 not in (0o100000, 0o040000, 0o120000):
                raise ValueError('Device nodes must be created at boot, not shipped')
            if any(name == p or name.startswith(p + '/') for p in image_policy.FOREIGN_MARKERS):
                raise ValueError('Foreign distribution in initramfs: ' + name)
            if name in ('etc/os-release', 'usr/lib/os-release') and mode & 0o170000 == 0o100000:
                distributions.append(image_policy.os_release(data.decode('utf8')))
            if 'ld-musl' in Path(name).name:
                raise ValueError('Foreign libc in initramfs')
            if data.startswith(b'\x7fELF') and (data[:6] != b'\x7fELF\x02\x02' or data[18:20] != b'\x00\x08'):
                raise ValueError('Non-MIPS64 big-endian ELF in initramfs: ' + name)
            if (name.startswith(('root/', 'home/', 'opt/dpfs/', 'opt/ffn-compat/', 'etc/ffn/', 'etc/ffn-ngfw/'))
                    or Path(name).name in ('shadow', 'gshadow', 'authorized_keys', 'machine-id')
                    or re.search(r'(ssh_host_.*_key|id_rsa|id_ed25519)$', name)
                    or private_key_material(data)):
                raise ValueError('Machine/vendor state in initramfs: ' + name)


def audit_root(root):
    """Reject machine state and secrets, rather than silently sanitizing a live root."""
    forbidden = ('root/.ssh', 'home', 'opt/dpfs', 'opt/ffn-compat',
                 'var/lib/ffn', 'var/lib/ffn-ngfw', 'etc/ffn/planes',
                 'etc/ffn-ngfw', 'var/log', 'run', 'tmp')
    for item in root.rglob('*'):
        name = item.relative_to(root).as_posix()
        if any(name == p or name.startswith(p + '/') for p in forbidden) and not item.is_dir():
            raise ValueError('Machine/vendor state in seed: ' + name)
        if item.is_symlink():
            # Absolute filesystem links are valid in a rootfs, but must never
            # be followed by the builder's host-side file operations.
            continue
        if not item.is_file():
            continue
        if any(name == p or name.startswith(p + '/') for p in forbidden):
            raise ValueError('Machine/vendor state in seed: ' + name)
        if (item.name in ('authorized_keys', 'machine-id', 'id_rsa', 'id_ed25519')
                or (item.name.startswith('ssh_host_') and item.name.endswith('_key'))):
            if item.stat().st_size:
                raise ValueError('Machine identity in seed: ' + name)
        if item.name in ('shadow', 'gshadow'):
            for row in item.read_text().splitlines():
                fields = row.split(':')
                if len(fields) < 2 or fields[1] not in ('!', '*', '!!', '!*'):
                    raise ValueError('Unlocked account/password in seed: ' + name)
        with item.open('rb') as stream:
            header = stream.read(20)
        if header.startswith(b'\x7fELF'):
            elf(item)
        if item.stat().st_size < 16 * 1024 * 1024:
            data = item.read_bytes()
            if private_key_material(data):
                raise ValueError('Private key material: ' + name)


def safe_install(source, root, destination):
    target = root / destination
    for parent in [target, *target.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError('Image overlay crosses symlink: ' + destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(0o755 if destination.endswith('.py') else 0o644)


def archive_git(repo, dest, commit):
    run(['git', '-C', repo, 'archive', '--format=tar', '--output', dest, commit])


def image_owner(member, owners):
    # Preserve service-account ownership from the seed without chown on the
    # builder. Files introduced by FFN belong to root, never the runner UID.
    original = owners.get(member.name.removeprefix('rootfs/')) if member.name.startswith('rootfs/') else None
    member.uid, member.gid = original if original else (0, 0)
    member.uname = member.gname = ''
    return member


def check_host():
    if os.name != 'posix' or not hasattr(tarfile, 'data_filter'):
        raise ValueError('Linux with Python 3.12+ tar data filtering required')


def check_kernel_config(conf, role):
    values = dict(re.findall(r'^(CONFIG_[A-Za-z0-9_]+)=([ym])$', conf, re.M))
    for symbol in ('64BIT', 'CPU_BIG_ENDIAN', 'CAVIUM_OCTEON_SOC', 'CGROUPS', 'DEVTMPFS'):
        if values.get('CONFIG_' + symbol) != 'y':
            raise ValueError('Missing kernel requirement: ' + symbol)
    if role == 'cp':
        for symbol in ('I2C', 'I2C_OCTEON', 'I2C_CHARDEV', 'I2C_MUX', 'I2C_MUX_PCA954x', 'DEVMEM'):
            value = values.get('CONFIG_' + symbol)
            if value not in ('y', 'm') or (value == 'm' and values.get('CONFIG_MODULES') != 'y'):
                raise ValueError('Missing CP cooling kernel requirement: ' + symbol)


def build(config, platform, core, out):
    check_host()
    if config.get('schema') != 1 or config.get('redistributable_inputs_reviewed') is not True:
        raise ValueError('Reviewed redistributable build inputs required')
    if set(config['planes']) != {'cp', 'dp'}:
        raise ValueError('Both CP and DP input profiles required')
    jobs = config.get('jobs', 4)
    if type(jobs) is not int or not 1 <= jobs <= 64:
        raise ValueError('Invalid parallel build count')
    platform_rev, core_rev = revision(platform), revision(core)
    out.mkdir(parents=True, exist_ok=False)
    assets = []
    provenance = {}
    archive_git(platform, out / 'platform-source.tar', platform_rev)
    archive_git(core, out / 'core-source.tar', core_rev)
    with tempfile.TemporaryDirectory(prefix='ffn-octeon-build-') as tmp:
        work = Path(tmp).resolve()
        for role in ('cp', 'dp'):
            cfg = config['planes'][role]
            kernel_repo = Path(cfg['kernel_repository']).resolve(strict=True)
            commit = cfg['kernel_commit']
            if not re.fullmatch('[0-9a-f]{40}', commit):
                raise ValueError('Kernel source must be pinned by full commit')
            source_archive = out / (role + '-kernel-source.tar')
            archive_git(kernel_repo, source_archive, commit)
            tree = work / (role + '-linux')
            tree.mkdir()
            with tarfile.open(source_archive) as tar:
                tar.extractall(tree, filter='data')
            kernel_version = image_policy.kernel(tree)
            seed = pinned_file(cfg['rootfs'])
            initramfs = pinned_file(cfg['initramfs'])
            audit_initramfs(initramfs)
            kernel_config = pinned_file(cfg['config'])
            cross = cfg['cross_compile']
            compiler = output([cross + 'gcc', '--version']).splitlines()[0]
            image_policy.compiler(output([cross + 'gcc', '-dumpmachine']), compiler)
            bundle = work / role
            root = bundle / 'rootfs'
            root.mkdir(parents=True)
            with tarfile.open(seed) as tar:
                tar.extractall(root, filter=rootfs_filter)
                owners = {m.name.removeprefix('./').rstrip('/'): (m.uid, m.gid) for m in tar.getmembers()}
            audit_root(root)
            operating_system = image_policy.debian_root(root, role)
            for required in ('usr/lib/systemd/systemd', 'usr/bin/python3'):
                executable = image_policy.root_path(root, required)
                if root not in executable.parents:
                    raise ValueError('Runtime executable escapes image root')
                elf(executable)
            module_dir = image_policy.root_path(root, 'lib/modules')
            if root not in module_dir.parents:
                raise ValueError('Kernel module directory escapes image root')
            if module_dir.exists() and any(module_dir.iterdir()):
                raise ValueError('Rootfs seed must not contain modules for a previous kernel')
            # Fixed source overlay is reviewed in git, not runner-supplied code.
            overlay = json.loads((platform / 'octeon/images/overlay.json').read_text())
            for origin, source, destination in overlay['common'] + overlay[role]:
                base = {'core': core, 'platform': platform}[origin]
                safe_install(base / source, root, destination)
                owners.pop(destination, None)
            marker = root / 'etc/ffn-image-role'
            if marker.parent.is_symlink() or marker.is_symlink():
                raise ValueError('Role marker crosses an image symlink')
            marker.parent.mkdir(exist_ok=True)
            marker.write_text(role + '\n')
            owners.pop('etc/ffn-image-role', None)
            shutil.copyfile(kernel_config, tree / '.config')
            run([tree / 'scripts/config', '--file', tree / '.config',
                 '--set-str', 'INITRAMFS_SOURCE', initramfs,
                 '--disable', 'LOCALVERSION_AUTO', '--set-str', 'LOCALVERSION',
                 '-ffn-' + role + '-' + platform_rev[:12]])
            make = ['make', '-C', tree, 'ARCH=mips', 'CROSS_COMPILE=' + cross]
            run(make + ['olddefconfig'])
            conf = (tree / '.config').read_text()
            check_kernel_config(conf, role)
            run(make + ['-j' + str(jobs), 'vmlinux', 'modules'])
            run(make + ['INSTALL_MOD_PATH=' + str(root), 'modules_install'])
            # Kernel build/source links point to the runner; source ships separately.
            for link in (root / 'lib/modules').glob('*/[bs]*'):
                if link.is_symlink() and link.name in ('build', 'source'):
                    link.unlink()
            run([cross + 'strip', '-o', bundle / 'vmlinux', tree / 'vmlinux'])
            elf(bundle / 'vmlinux')
            release = (tree / 'include/config/kernel.release').read_text().strip()
            run(['depmod', '-b', root, release])
            shutil.copyfile(tree / '.config', bundle / 'kernel.config')
            shutil.copyfile(initramfs, out / (role + '-initramfs.cpio'))
            # Redistributors must provide matching Debian/seed sources and notices.
            sources = pinned_file(cfg['corresponding_sources'])
            shutil.copyfile(sources, out / (role + '-userspace-sources.tar.xz'))
            audit_root(root)
            details = {'role': role, 'architecture': 'mips64eb', 'kernel_release': release,
                       'operating_system': operating_system, 'kernel_source_version': kernel_version,
                       'kernel_commit': commit, 'compiler': compiler,
                       'rootfs_sha256': sha(seed), 'initramfs_sha256': sha(initramfs),
                       'kernel_config_sha256': sha(bundle / 'kernel.config'),
                       'hardware_boot_verified': False, 'runtime_abi': 1,
                       'platform_commit': platform_rev, 'core_commit': core_rev}
            (bundle / 'image.json').write_text(json.dumps(details, indent=2) + '\n')
            name = 'ffn-pa5200-' + role + '.tar.xz'
            with tarfile.open(out / name, 'w:xz') as tar:
                for path in sorted(bundle.iterdir()):
                    tar.add(path, arcname=path.name, filter=lambda m: image_owner(m, owners))
            size = (out / name).stat().st_size
            if size > 2 * 1024**3:
                raise ValueError('Image exceeds GitHub release asset limit')
            assets.append(dict(role=role, name=name, size=size, sha256=sha(out / name), kernel_release=release))
            provenance[role] = details
    manifest = dict(schema=1, platform='pa5200', architecture='mips64eb', runtime_abi=1,
                    platform_commit=platform_rev, core_commit=core_rev, assets=assets,
                    build=provenance, hardware_boot_verified=False)
    (out / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
    (out / 'SHA256SUMS').write_text(''.join(sha(p) + '  ' + p.name + '\n' for p in sorted(out.iterdir()) if p.is_file()))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', required=True, type=Path)
    p.add_argument('--platform', required=True, type=Path)
    p.add_argument('--core', required=True, type=Path)
    p.add_argument('--out', required=True, type=Path)
    a = p.parse_args()
    build(json.loads(a.config.read_text()), a.platform.resolve(), a.core.resolve(), a.out.resolve())
