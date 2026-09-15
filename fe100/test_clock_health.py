import unittest
from ffn_fe100_lookup_health import analyze, selected, PAIR_NAMES, CLOCK_MONITORS


class Health(unittest.TestCase):
    def test_disabled_monitor_is_unknown_not_clock_failure(self):
        regs = {}
        for names in PAIR_NAMES.values():
            for name in names:
                regs[name+'_stats_ctr_no_rd_clr'] = {'raw':0, 'fields':{}}
        regs['tdi_init_status'] = {'raw':0x3800000, 'fields':{
            'tcam_1x_clk':1, 'tcam_2x_clk':1, 'dram_ddr_clk':0, 'dram_pclk':0}}
        for key, name in CLOCK_MONITORS.items():
            regs[name] = {'raw':0, 'fields':{'en':int(key.startswith('tcam'))}}
            self.assertTrue(selected(name))
        result = analyze([{'registers':regs}, {'registers':regs}])
        self.assertEqual(result['external_clock_status']['tcam_1x_clk'], 1)
        self.assertIsNone(result['external_clock_status']['dram_ddr_clk'])
        self.assertEqual(result['external_clock_status_raw']['dram_ddr_clk'], 0)
        self.assertFalse(result['offload_verified'])
        self.assertFalse(selected('tdi_some_stats_rd_clr'))


if __name__ == '__main__': unittest.main()
