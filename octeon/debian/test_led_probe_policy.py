import unittest
from ffn_port_led_probe import program


class ProbeProgramTests(unittest.TestCase):
    def test_fits_program_ram_and_sends_exact_length(self):
        for bits in (8, 24, 64):
            for one in (False, True):
                code = program(bits, one)
                self.assertLessEqual(len(code), 256)
                self.assertEqual(code[-2:], [0x3a, bits])
                self.assertEqual(code[:-2], [0x32, 0xf if one else 0xe, 0x87] * bits)

    def test_rejects_unbounded_or_ambiguous_patterns(self):
        for bits, one in ((0,True),(128,True),(64,1),(64,None)):
            with self.assertRaises(ValueError):
                program(bits,one)


if __name__ == '__main__':
    unittest.main()
