import hashlib
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import build_images as b
import image_policy as policy


def cpio_entry(name, content=b'', mode=0o100644):
    name = name.encode() + b'\0'
    fields = [1, mode, 0, 0, 1, 0, len(content), 0, 0, 0, 0, len(name), 0]
    header = b'070701' + ''.join('%08x' % i for i in fields).encode()
    head = header + name
    return head + b'\0' * (-len(head) % 4) + content + b'\0' * (-len(content) % 4)


def debian_metadata(root):
    files = {'usr/lib/os-release': 'ID=debian\nVERSION_CODENAME=sid\n',
             'var/lib/dpkg/status': '\n\n'.join('Package: '+n+'\nStatus: install ok installed\nArchitecture: mips64\nVersion: 1' for n in ('base-files', 'libc6', 'systemd', 'python3')),
             'var/lib/dpkg/info/systemd.list': '/usr/lib/systemd/systemd\n',
             'var/lib/dpkg/info/python3.list': '/usr/bin/python3\n'}
    for name, data in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data)


class BuildInputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_elf_endianness_and_machine(self):
        p = self.root / 'elf'
        good = b'\x7fELF\x02\x02' + b'\0'*12 + b'\x00\x08'
        p.write_bytes(good)
        b.elf(p)
        for invalid in (good[:5]+b'\x01'+good[6:], good[:-2]+b'\x00\x3e', b'bad'):
            p.write_bytes(invalid)
            with self.assertRaises(ValueError):
                b.elf(p)

    def test_pinned_seed_tampering(self):
        p = self.root / 'seed'
        p.write_bytes(b'clean')
        item = dict(path=str(p), sha256=hashlib.sha256(b'clean').hexdigest())
        self.assertEqual(b.pinned_file(item), p.resolve())
        p.write_bytes(b'changed')
        with self.assertRaises(ValueError):
            b.pinned_file(item)

    def test_seed_rejects_identity_and_config(self):
        for name, value in [('etc/machine-id', 'identity'), ('root/.ssh/authorized_keys', 'key'),
                            ('var/lib/ffn-ngfw/running-config.xml', 'configuration'),
                            ('etc/ssh/ssh_host_ed25519_key', 'key'),
                            ('usr/share/secret', '-----BEGIN OPENSSH PRIVATE KEY-----')]:
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(value)
            with self.subTest(name=name), self.assertRaises(ValueError):
                b.audit_root(self.root)
            p.unlink()

    def test_foreign_executable_anywhere_in_root_rejected(self):
        p = self.root / 'optional-tool'
        p.write_bytes(b'\x7fELF\x02\x01' + b'\0'*12 + b'\x3e\x00')
        with self.assertRaises(ValueError): b.audit_root(self.root)
        p.write_bytes(b'\x7fELF\x02\x02' + b'\0'*12 + b'\x00\x08')
        b.audit_root(self.root)

    def test_accounts_must_be_locked(self):
        p = self.root / 'shadow'
        p.write_text('root:!:0:0:99999:7:::\n')
        b.audit_root(self.root)
        for password in ('', '$6$credential'):
            p.write_text('root:'+password+':0:0:99999:7:::\n')
            with self.assertRaises(ValueError):
                b.audit_root(self.root)

    def test_initramfs_bounds_and_secrets(self):
        p = self.root / 'initramfs'
        good = cpio_entry('init', b'clean') + cpio_entry('usr/lib/os-release', b'ID=debian\n') + cpio_entry('TRAILER!!!')
        p.write_bytes(good)
        b.audit_initramfs(p)
        for data in (good[:-10], good + good, cpio_entry('../../escape') + cpio_entry('TRAILER!!!'),
                     cpio_entry('etc/shadow', b'secret') + cpio_entry('TRAILER!!!'),
                     cpio_entry('init', b'-----BEGIN RSA PRIVATE KEY-----') + cpio_entry('TRAILER!!!')):
            p.write_bytes(data)
            with self.subTest(data=data[:20]), self.assertRaises(ValueError):
                b.audit_initramfs(p)

    def test_debian_root_rejects_foreign_and_unowned_userspace(self):
        debian_metadata(self.root)
        self.assertEqual(policy.debian_root(self.root)['architecture'], 'mips64')
        os_release = self.root / 'usr/lib/os-release'
        for distro in ('centos', 'openwrt', 'buildroot', 'ubuntu'):
            os_release.write_text('ID='+distro+'\n')
            with self.subTest(distro=distro), self.assertRaises(ValueError):
                policy.debian_root(self.root)
        os_release.write_text('ID=debian\n')
        status = self.root / 'var/lib/dpkg/status'
        text = status.read_text()
        status.write_text(text.replace('mips64', 'mips64el'))
        with self.assertRaises(ValueError): policy.debian_root(self.root)
        status.write_text(text)
        (self.root / 'var/lib/dpkg/info/python3.list').unlink()
        with self.assertRaises(ValueError): policy.debian_root(self.root)

    def test_foreign_marker_cannot_be_hidden_by_debian_os_release(self):
        debian_metadata(self.root)
        p = self.root / 'etc/openwrt_release'
        p.parent.mkdir(); p.write_text('OpenWrt')
        with self.assertRaises(ValueError): policy.debian_root(self.root)
        p.unlink()
        if os.name == 'posix':
            p.symlink_to('/missing-root')
            with self.assertRaises(ValueError): policy.debian_root(self.root)

    @unittest.skipUnless(os.name == 'posix', 'Linux image symlinks')
    def test_rootfs_symlink_resolution_never_uses_host_root(self):
        (self.root / 'lib').symlink_to('/usr/lib')
        self.assertEqual(policy.root_path(self.root, '/lib/os-release'), self.root / 'usr/lib/os-release')
        (self.root / 'escape').symlink_to('../../outside')
        with self.assertRaises(ValueError): policy.root_path(self.root, 'escape')

    def test_legacy_kernels_and_openwrt_compilers_rejected(self):
        p = self.root / 'Makefile'
        p.write_text('VERSION = 6\nPATCHLEVEL = 18\nSUBLEVEL = 49\n')
        self.assertEqual(policy.kernel(self.root), '6.18.49')
        p.write_text('VERSION = 4\nPATCHLEVEL = 9\nSUBLEVEL = 57\n')
        with self.assertRaises(ValueError): policy.kernel(self.root)
        policy.compiler('mips64-linux-gnuabi64', 'GCC 14.4')
        for target, version in [('mips64-openwrt-linux-musl', 'GCC 13.3'), ('mips64el-linux-gnuabi64', 'GCC'), ('mips64-linux', 'OpenWrt GCC')]:
            with self.subTest(target=target), self.assertRaises(ValueError): policy.compiler(target, version)

    def test_foreign_or_unidentified_initramfs_rejected(self):
        p = self.root / 'init.cpio'
        for distro in ('centos', 'openwrt', 'buildroot', ''):
            data = cpio_entry('init', b'clean')
            if distro: data += cpio_entry('etc/os-release', ('ID='+distro+'\n').encode())
            p.write_bytes(data+cpio_entry('TRAILER!!!'))
            with self.subTest(distro=distro), self.assertRaises(ValueError): b.audit_initramfs(p)

    def test_overlay_is_scoped_and_has_plane_agents(self):
        data = json.loads(Path(__file__).with_name('overlay.json').read_text())
        for role in ('cp', 'dp'):
            entries = data['common'] + data[role]
            destinations = [row[2] for row in entries]
            self.assertEqual(len(destinations), len(set(destinations)))
            self.assertIn('usr/local/sbin/ffn_'+role+'_agent.py', destinations)
            for origin, source, dest in entries:
                self.assertIn(origin, ('core', 'platform'))
                self.assertFalse(Path(source).is_absolute())
                self.assertFalse(Path(dest).is_absolute())
                self.assertNotIn('..', Path(source).parts + Path(dest).parts)

    @unittest.skipUnless(hasattr(tarfile, 'data_filter'), 'Python tar data filtering required')
    def test_build_pair_with_simulated_compiler(self):
        # Exercise real rootfs extraction, auditing, overlay, packaging and hashes.
        # Only git/compiler execution is simulated, not the package operations.
        good_elf = b'\x7fELF\x02\x02' + b'\0'*12 + b'\x00\x08'
        platform = self.root / 'platform'
        (platform / 'octeon/images').mkdir(parents=True)
        (platform / 'agent.py').write_text('# agent\n')
        overlay = dict(common=[], cp=[['platform', 'agent.py', 'usr/local/sbin/cp.py']],
                       dp=[['platform', 'agent.py', 'usr/local/sbin/dp.py']])
        (platform / 'octeon/images/overlay.json').write_text(json.dumps(overlay))
        root = self.root / 'seed'
        debian_metadata(root)
        for binary in ('usr/lib/systemd/systemd', 'usr/bin/python3'):
            target = root / binary
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(good_elf)
        seed = self.root / 'seed.tar'
        def seed_owner(member):
            member.uid, member.gid = 1234, 5678
            return member
        with tarfile.open(seed, 'w') as tar:
            tar.add(root / 'usr', arcname='usr', filter=seed_owner)
            tar.add(root / 'var', arcname='var', filter=seed_owner)
        init = self.root / 'init.cpio'
        init.write_bytes(cpio_entry('init', b'clean') + cpio_entry('usr/lib/os-release', b'ID=debian\n') + cpio_entry('TRAILER!!!'))
        config = self.root / 'config'
        config.write_text(''.join('CONFIG_'+s+'=y\n' for s in
                                 ('64BIT', 'CPU_BIG_ENDIAN', 'CAVIUM_OCTEON_SOC', 'CGROUPS', 'DEVTMPFS')))
        def pin(p):
            return dict(path=str(p), sha256=b.sha(p))
        inputs = dict(kernel_repository=str(platform), kernel_commit='c'*40,
                      cross_compile='/toolchain/mips64-', rootfs=pin(seed),
                      initramfs=pin(init), config=pin(config), corresponding_sources=pin(seed))
        profile = dict(schema=1, redistributable_inputs_reviewed=True,
                       planes={'cp': inputs, 'dp': inputs})
        def archive(repo, dest, commit):
            with tarfile.open(dest, 'w') as tar:
                if dest.name.endswith('-kernel-source.tar'):
                    import io
                    data = b'VERSION = 6\nPATCHLEVEL = 18\nSUBLEVEL = 49\n'
                    member = tarfile.TarInfo('Makefile'); member.size = len(data)
                    tar.addfile(member, io.BytesIO(data))
        def compiler(args, **kw):
            args = list(map(str, args))
            if args[0].endswith('strip'):
                shutil.copyfile(args[-1], args[2])
            if args[0] == 'make':
                tree = Path(args[2])
                (tree / 'include/config').mkdir(parents=True, exist_ok=True)
                (tree / 'include/config/kernel.release').write_text('6.18-test')
                (tree / 'vmlinux').write_bytes(good_elf)
                for arg in args:
                    if arg.startswith('INSTALL_MOD_PATH='):
                        (Path(arg.split('=', 1)[1]) / 'lib/modules/6.18-test').mkdir(parents=True)
        import shutil
        out = self.root / 'out'
        with patch.object(b, 'revision', return_value='a'*40), patch.object(b, 'archive_git', side_effect=archive), \
                patch.object(b, 'run', side_effect=compiler), patch.object(b, 'output', side_effect=lambda args: 'mips64-linux-gnuabi64' if args[-1] == '-dumpmachine' else 'gcc test'), \
                patch.object(b, 'check_host'):
            b.build(profile, platform, platform, out)
        manifest = json.loads((out / 'manifest.json').read_text())
        self.assertEqual({a['role'] for a in manifest['assets']}, {'cp', 'dp'})
        for asset in manifest['assets']:
            self.assertEqual(b.sha(out / asset['name']), asset['sha256'])
            with tarfile.open(out / asset['name']) as tar:
                names = tar.getnames()
                self.assertIn('vmlinux', names)
                self.assertIn('rootfs/usr/local/sbin/'+asset['role']+'.py', names)
                self.assertIn('rootfs/lib/modules/6.18-test', names)
                self.assertEqual(tar.getmember('rootfs/usr/bin/python3').uid, 1234)
                self.assertEqual(tar.getmember('rootfs/usr/local/sbin/'+asset['role']+'.py').uid, 0)


if __name__ == '__main__':
    unittest.main()
