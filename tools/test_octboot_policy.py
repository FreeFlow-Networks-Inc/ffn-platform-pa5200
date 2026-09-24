import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch, Mock


with patch.dict(sys.modules, {'ffn_octdram': types.ModuleType('ffn_octdram')}):
    spec = importlib.util.spec_from_file_location('octboot_policy_test_target', Path(__file__).with_name('ffn_octboot.py'))
    boot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(boot)


class KernelPolicyTests(unittest.TestCase):
    def test_current_image_required_even_if_renamed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'renamed'
            elf = b'\x7fELF\x02\x02' + b'\0'*12 + b'\x00\x08'
            for suffix in (b'Linux version 4.9.57', b'unknown', b'Linux version 6.18.49 Linux version 4.9.57'):
                path.write_bytes(elf + suffix)
                with self.assertRaises(ValueError): boot.kernel_bytes(path)
            path.write_bytes(elf + b'Linux version 6.18.49')
            self.assertEqual(boot.kernel_bytes(path), path.read_bytes())
            path.write_bytes(b'\x7fELF\x02\x01' + b'\0'*12 + b'\x3e\x00Linux version 6.18.49')
            with self.assertRaises(ValueError): boot.kernel_bytes(path)

    def test_bad_kernel_stops_before_console_or_hardware(self):
        with patch.object(sys, 'argv', ['boot', '--kernel', '/missing']), \
             patch.object(boot, 'kernel_bytes', side_effect=ValueError('rejected')), \
             patch.object(boot, 'prompt_ok', side_effect=AssertionError('console touched')), \
             patch.object(boot, 'os', types.SimpleNamespace(path=types.SimpleNamespace(exists=Mock(side_effect=AssertionError('broker probed'))))):
            self.assertEqual(boot.main(), 2)

    def test_no_stage_still_verifies_dram_before_boot(self):
        with patch.object(sys, 'argv', ['boot', '--kernel', '/reviewed', '--no-stage']), \
             patch.object(boot, 'kernel_bytes', return_value=b'reviewed-kernel'), \
             patch.object(boot, 'os', types.SimpleNamespace(path=types.SimpleNamespace(exists=lambda p: True))), \
             patch.object(boot, 'prompt_ok', return_value=(True, 'test prompt')), \
             patch.object(boot.od, 'WindowedDram', create=True) as window, \
             patch.object(boot, 'fifo') as console:
            window.return_value.__enter__.return_value.read.return_value = b'wrong-kernel'
            self.assertEqual(boot.main(), 1)
            window.return_value.__enter__.return_value.write.assert_not_called()
            console.assert_not_called()


if __name__ == '__main__':
    unittest.main()
