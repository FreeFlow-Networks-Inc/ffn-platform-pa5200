from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import build_transport as transport
import runtime_inventory as inventory


def executable():
    data = bytearray(120)
    data[:6] = b'\x7fELF\x02\x02'
    data[16:20] = b'\x00\x02\x00\x08'
    data[32:40] = (64).to_bytes(8, 'big')
    data[54:58] = b'\x00\x38\x00\x01'
    data[64:68] = (1).to_bytes(4, 'big')
    return data


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_wrong_endian_dynamic_and_malformed_elf_rejected(self):
        good = executable()
        transport.static_mips(good)
        for start, value in [(5, b'\x01'), (16, b'\x00\x01'), (64, b'\x00\x00\x00\x03'), (56, b'\xff\xff')]:
            bad = good.copy()
            bad[start:start+len(value)] = value
            with self.assertRaises(ValueError): transport.static_mips(bad)

    @unittest.skipUnless(shutil.which('dpkg-deb'), 'Linux Debian package tools required')
    def test_payload_audit_rejects_activation_and_unexpected_files(self):
        stage = self.root / 'stage'
        control = stage / 'DEBIAN'
        control.mkdir(parents=True)
        (control / 'control').write_text('Package: ffn-octeon-transport\nVersion: 1\nArchitecture: mips64\nMaintainer: Test <test@example.invalid>\nDescription: test\nBuilt-Using: glibc (= 2.43)\n')
        for name in transport.BINARIES:
            p = stage / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(executable())
            p.chmod(0o755)
        package = self.root / 'test.deb'
        def build():
            subprocess.run(['dpkg-deb', '--root-owner-group', '--build', str(stage), str(package)], check=True, stdout=subprocess.DEVNULL)
        build()
        self.assertEqual(set(transport.audit_deb(package)['executables']), transport.BINARIES)
        script = control / 'postinst'
        script.write_text('#!/bin/sh\nexit 0\n'); script.chmod(0o755)
        build()
        with self.assertRaises(ValueError): transport.audit_deb(package)
        script.unlink()
        other = stage / 'etc/ffn/lab.conf'
        other.parent.mkdir(parents=True); other.write_text('not a transport')
        build()
        with self.assertRaises(ValueError): transport.audit_deb(package)

    def test_inventory_distinguishes_planes_and_ignores_foreign_packages(self):
        index = self.root / 'Packages'
        index.write_text('Package: python3\nVersion: 1\nArchitecture: mips64el\n\nPackage: nfs-kernel-server\nVersion: 2\nArchitecture: mips64\n')
        cp = inventory.report('cp', [index])
        dp = inventory.report('dp', [index])
        self.assertIn('python3', cp['missing_from_repository'])
        self.assertNotIn('nfs-kernel-server', cp['missing_from_repository'])
        self.assertNotIn('nfs-kernel-server', [p['name'] for p in dp['packages']])
        self.assertIn('nftables', dp['missing_from_repository'])

    def test_unconfigured_or_foreign_root_does_not_pass(self):
        status = self.root / 'var/lib/dpkg/status'
        status.parent.mkdir(parents=True)
        status.write_text('Package: python3\nVersion: 1\nArchitecture: mips64\nStatus: install ok unpacked\n')
        self.assertIn('python3', inventory.report('cp', root=self.root)['missing_from_root'])
        status.write_text(status.read_text().replace('mips64', 'amd64').replace('unpacked', 'installed'))
        with self.assertRaises(ValueError): inventory.report('cp', root=self.root)

    def test_metadata_continuations_and_duplicate_fields(self):
        self.assertIn(' bar', inventory.paragraphs('Package: a\nDepends: foo,\n bar\n')[0]['Depends'])
        with self.assertRaises(ValueError): inventory.paragraphs('Package: a\nPackage: b\n')


if __name__ == '__main__':
    unittest.main()
