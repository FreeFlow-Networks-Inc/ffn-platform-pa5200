import unittest
from ffn_fe100_ddr_diagnostics import eye_widths, measurements_present
from ffn_fe100_calibration_log import summarize


class DiagnosticsTests(unittest.TestCase):
    def test_zero_encoding_does_not_qualify_unmeasured_group(self):
        self.assertEqual(eye_widths([0]*8), [64]*16)
        self.assertFalse(measurements_present([0]*8, {'0x19':0,'0x1a':0}))
        self.assertTrue(measurements_present([0]*8, {'0x19':1,'0x1a':0}))
        self.assertEqual(eye_widths([0x3f01]), [1,63])

    def test_failed_stage_survives_later_success(self):
        report=summarize('''.. run_init_cal_algorithm - READ_CENTERING (FE100_DRAM_FDT1) Running Init Calibration Algorithm
Received Initial Calibration Error: Error Register Value = 0x808
run_init_cal_algorithm:4295 failed-rc=-1
.. run_init_cal_algorithm - WRITE_CENTERING (FE100_DRAM_FDT1) Running Init Calibration Algorithm
Exiting run_init_cal_algorithm.
.. run_init_cal_algorithm - COARSE_READ (FE100_DRAM_FDT1) Running Init Calibration Algorithm
''')
        self.assertEqual(report['failed_algorithms'], ['READ_CENTERING'])
        self.assertEqual([s['outcome'] for s in report['stages']],
                         ['failed','returned_success','incomplete'])
        self.assertEqual(report['stages'][0]['error_registers'],['0x808'])
        self.assertFalse(report['training_verified'])


if __name__ == '__main__': unittest.main()
