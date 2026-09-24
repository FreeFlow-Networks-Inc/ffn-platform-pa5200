import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import ffn_fe100 as fe


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.driver = self.root/'ffn_fe100'
        self.driver.mkdir()
        self.device = self.root/'device0'
        self.device.mkdir()
        (self.device/'driver').symlink_to(self.driver, target_is_directory=True)
        (self.device/'vendor').write_text('0xfeed\n')
        (self.device/'device').write_text('0xfe1c\n')
        (self.device/'resource').write_text('10000000 100fffff 200\n')
        (self.device/'config').write_bytes(bytes(4)+b'\x02\x00')
        with (self.device/'resource0').open('wb') as f:
            f.truncate(0x100000)
        self.override = patch.object(fe, 'SYSFS', str(self.device))
        self.override.start()
        self.addCleanup(self.override.stop)

    def test_bar_relative_mapping_and_little_endian_registers(self):
        io = fe.Fe100()
        try:
            io.write32(0x48018, 0x12345678)
            self.assertEqual(io.read32(0x48018), 0x12345678)
            self.assertEqual(io.map[0x48018:0x4801c], b'\x78\x56\x34\x12')
            for offset in (-4, 1, 0x100000):
                with self.assertRaises(ValueError):io.read32(offset)
                with self.assertRaises(ValueError):io.write32(offset, 0)
        finally:io.close()

    def test_unbound_device_fails_without_raw_memory_fallback(self):
        (self.device/'driver').unlink()
        with self.assertRaisesRegex(RuntimeError, 'must be bound'):fe.Fe100()

    def test_wrong_identity_or_bar_rejected(self):
        (self.device/'vendor').write_text('0x1234')
        with self.assertRaisesRegex(RuntimeError, 'identity'):fe.Fe100()
        (self.device/'vendor').write_text('0xfeed')
        (self.device/'resource').write_text('10000000 1000ffff 200\n')
        with self.assertRaisesRegex(RuntimeError, 'geometry'):fe.Fe100()

    def test_disabled_memory_decode_rejected(self):
        (self.device/'config').write_bytes(bytes(6))
        with self.assertRaisesRegex(RuntimeError, 'decode'):fe.Fe100()


if __name__ == '__main__':unittest.main()
