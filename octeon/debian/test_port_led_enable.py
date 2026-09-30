import unittest
from ffn_port_led_enable import enable, status


class PortLedEnableTests(unittest.TestCase):
    def test_all_byte_values_preserve_unrelated_bits_and_are_idempotent(self):
        for value in range(256):
            csr = bytearray(range(32)); csr[0] = 0x13; csr[8] = value
            before = bytes(csr)
            self.assertEqual(status(csr)['output_enabled'], bool(value & 1))
            self.assertEqual(bytes(csr), before)
            self.assertTrue(enable(csr)['output_enabled'])
            self.assertEqual(csr[:8], before[:8]); self.assertEqual(csr[9:], before[9:])
            self.assertEqual(csr[8], value | 1)
            applied = bytes(csr); enable(csr); self.assertEqual(bytes(csr), applied)

    def test_unknown_cpld_rejected_before_writes(self):
        csr = bytearray(range(32)); before = bytes(csr)
        with self.assertRaisesRegex(RuntimeError, 'unrecognized'): enable(csr)
        self.assertEqual(bytes(csr), before)


if __name__ == '__main__': unittest.main()
