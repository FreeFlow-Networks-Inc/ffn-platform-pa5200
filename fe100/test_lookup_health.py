import copy
import unittest
from ffn_fe100_lookup_health import analyze, PAIR_NAMES, selected


def baseline():
    regs = {n + '_stats_ctr_no_rd_clr': {'raw': 100, 'fields': {'val': 100}}
            for pair in PAIR_NAMES.values() for n in pair}
    regs['acl_rx_pca_fifo_status'] = {'raw': 0x40000, 'fields': {'level': 0, 'hi_mark': 4}}
    regs['acl_cr_flow_control_status'] = {'raw': 4, 'fields': {'lif_acl_pca_imp_fc_err': 1}}
    return {'registers': regs}


class LookupHealthTests(unittest.TestCase):
    def test_never_reads_clearing_alias(self):
        self.assertTrue(selected('tlu_cr_flu_status_rx_stats_ctr_no_rd_clr'))
        self.assertFalse(selected('tlu_cr_flu_status_rx_stats_ctr_rd_clr'))
        self.assertFalse(selected('acl_ia_cmd'))

    def test_idle_with_historical_flags_is_not_offload_success(self):
        first = baseline()
        result = analyze([first, copy.deepcopy(first)])
        self.assertFalse(result['offload_verified'])
        self.assertFalse(result['acl_lookup_observed'])
        self.assertEqual(result['nonempty_queues'], [])
        self.assertFalse(result['flow_control_flags'][0]['current'])

    def test_completed_lookups_do_not_hide_pending_packets(self):
        first, last = baseline(), baseline()
        for sample in (first, last):
            sample['registers']['acl_rx_pca_fifo_status']['fields']['level'] = 24
        for key in PAIR_NAMES['acl_lookups']:
            last['registers'][key+'_stats_ctr_no_rd_clr']['raw'] += 34
        last['registers'][PAIR_NAMES['acl_packets'][0]+'_stats_ctr_no_rd_clr']['raw'] += 24
        result = analyze([first, last])
        self.assertEqual(result['counter_pairs']['acl_lookups']['final_balance_mod32'], 0)
        self.assertEqual(result['counter_pairs']['acl_packets']['final_balance_mod32'], 24)
        self.assertTrue(result['nonempty_queues'][0]['nonempty_all_samples'])
        self.assertFalse(result['offload_verified'])

    def test_counter_wrap(self):
        first, last = baseline(), baseline()
        key = PAIR_NAMES['acl_lookups'][0]+'_stats_ctr_no_rd_clr'
        first['registers'][key]['raw'] = 0xfffffffe
        last['registers'][key]['raw'] = 3
        self.assertEqual(analyze([first, last])['counter_pairs']['acl_lookups']['received_delta_mod32'], 5)


if __name__ == '__main__':
    unittest.main()
