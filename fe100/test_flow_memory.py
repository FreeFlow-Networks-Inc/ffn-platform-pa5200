import ctypes as C
import struct
import unittest
from ffn_fe100_flow_memory import enable_fdt1_init_pattern


class PatternCorrection(unittest.TestCase):
    def config(self):
        cfg=C.create_string_buffer(2812)
        for k,v in {20:1,24:1,28:1,108:6,148:1,152:1,164:1}.items():
            struct.pack_into('>I',cfg,964+k,v)
        return cfg

    def test_only_missing_pattern_flag_changes(self):
        cfg=self.config(); before=bytes(cfg)
        self.assertEqual(enable_fdt1_init_pattern(cfg)['previous'],0)
        after=bytes(cfg)
        self.assertEqual(after[:1152],before[:1152])
        self.assertEqual(after[1156:],before[1156:])
        self.assertEqual(struct.unpack_from('>I',after,1152)[0],1)
        self.assertEqual(enable_fdt1_init_pattern(cfg)['previous'],1)

    def test_other_profiles_and_disabled_calibration_rejected(self):
        for offset in (20,24,28,108,148,152,164):
            cfg=self.config();struct.pack_into('>I',cfg,964+offset,0)
            with self.assertRaises(ValueError): enable_fdt1_init_pattern(cfg)


if __name__=='__main__': unittest.main()
