import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import build_images as b


def cpio_entry(name, content=b'', mode=0o100644):
    name = name.encode() + b'\0'
    fields = [1, mode, 0, 0, 1, 0, len(content), 0, 0, 0, 0, len(name), 0]
    header = b'070701' + ''.join('%08x' % i for i in fields).encode()
    head = header + name
    return head + b'\0' * (-len(head) % 4) + content + b'\0' * (-len(content) % 4)


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
        good = cpio_entry('init', b'clean') + cpio_entry('TRAILER!!!')
        p.write_bytes(good)
        b.audit_initramfs(p)
        for data in (good[:-10], good + good, cpio_entry('../../escape') + cpio_entry('TRAILER!!!'),
                     cpio_entry('etc/shadow', b'secret') + cpio_entry('TRAILER!!!'),
                     cpio_entry('init', b'-----BEGIN RSA PRIVATE KEY-----') + cpio_entry('TRAILER!!!')):
            p.write_bytes(data)
            with self.subTest(data=data[:20]), self.assertRaises(ValueError):
                b.audit_initramfs(p)

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
        init = self.root / 'init.cpio'
        init.write_bytes(cpio_entry('init', b'clean') + cpio_entry('TRAILER!!!'))
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
            with tarfile.open(dest, 'w'):
                pass
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
                patch.object(b, 'run', side_effect=compiler), patch.object(b, 'output', return_value='gcc test'), \
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
