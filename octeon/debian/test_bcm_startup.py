"""Regression checks for service restarts after the kernel ring buffer wraps."""
import shutil
import subprocess
import unittest
from pathlib import Path

CONFIRMATION = 'FFN: ffn_reserve 0x30000000+0x4000000 reserved, kept out of the allocator'

@unittest.skipUnless(shutil.which('sh'), 'POSIX shell required')
class StartupTests(unittest.TestCase):
    def check_guard(self, ring, journal):
        script = Path(__file__).with_name('ffn-bcm-debian.sh').read_text()
        guard = script.split('prepare)\n', 1)[1].split('    test -x ', 1)[0]
        mocks = "dmesg() { printf '%s\\n' "+repr(ring)+"; }\n"
        mocks += "journalctl() { [ \"$*\" = '--boot=0 --dmesg --no-pager --output=cat' ] || return 1; printf '%s\\n' "+repr(journal)+"; }\n"
        return subprocess.run(['sh','-c',mocks+guard],capture_output=True,text=True).returncode

    def test_ring_confirmation(self):
        self.assertEqual(self.check_guard(CONFIRMATION,''),0)

    def test_wrapped_ring_uses_current_boot_journal(self):
        self.assertEqual(self.check_guard('unrelated recent message',CONFIRMATION),0)

    def test_cmdline_or_missing_confirmation_rejected(self):
        for value in ('', 'Kernel command line: ffn_reserve=0x30000000,64M'):
            self.assertNotEqual(self.check_guard(value,value),0)

if __name__ == '__main__': unittest.main()
