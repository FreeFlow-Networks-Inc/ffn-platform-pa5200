import tempfile
import unittest
from pathlib import Path
from ffn_dp_boot_health import inspect_boot


class BootHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root, self.proc = base/'root', base/'proc'
        (self.root/'etc').mkdir(parents=True)
        (self.root/'etc/os-release').write_text('ID=debian\n')
        (self.proc/'1').mkdir(parents=True)
        (self.proc/'1/root').symlink_to(self.root)
        (self.proc/'1/exe').symlink_to('/usr/lib/systemd/systemd')

    def test_systemd_on_same_debian_root(self):
        self.assertTrue(inspect_boot(self.proc,self.root)['ready'])

    def test_debian_files_with_legacy_init_are_not_ready(self):
        (self.proc/'1/exe').unlink()
        (self.proc/'1/exe').symlink_to('/init')
        r=inspect_boot(self.proc,self.root)
        self.assertFalse(r['ready'])
        self.assertEqual(r['pid1_os'],'debian')
        self.assertIn('PID 1 has not handed control to systemd',r['reasons'])

    def test_staged_debian_copy_is_not_active_root(self):
        other=Path(self.tmp.name)/'stage'
        (other/'etc').mkdir(parents=True)
        (other/'etc/os-release').write_text('ID=debian\n')
        r=inspect_boot(self.proc,other)
        self.assertFalse(r['ready'])
        self.assertFalse(r['same_root_as_pid1'])

    def test_missing_proc_is_unknown_not_ready(self):
        r=inspect_boot(Path(self.tmp.name)/'missing',self.root)
        self.assertFalse(r['ready'])
        self.assertTrue(r['reasons'])

    def test_other_os_not_ready(self):
        (self.root/'etc/os-release').write_text('ID=buildroot\n')
        self.assertFalse(inspect_boot(self.proc,self.root)['ready'])


if __name__ == '__main__': unittest.main()
