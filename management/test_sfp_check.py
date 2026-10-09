import json
import tempfile
import unittest
from pathlib import Path
import ffn_sfp_check as check

MODULE_1G = dict(vendor='OEM', part='GLC-LH-SMD', wavelength_nm=1310, optical=True, speeds=[1000])
MODULE_10G = dict(vendor='OEM', part='SFP-10G-LR', wavelength_nm=1310, optical=True, speeds=[10000])
GOOD = dict(diagnostics_available=True, tx_power_dbm=-5.2, rx_power_dbm=-10.2, tx_bias_ma=23.3, tx_disable=False, rx_los=False, tx_fault=False)
LINK = dict(mac_enabled=True, link=True, interface='BCM_PORT_IF_GMII', link_mode='1000BASE-X', autoneg=True, configured_speed='1000')
ATTACHED = dict(state='enabled', enabled=True, pending=None, epoch='e')


def run(**changes):
    args = dict(cage=dict(present=True, tx_disable=False), module=MODULE_1G, diagnostics=dict(GOOD), link=dict(LINK),
                attached=dict(ATTACHED), committed=dict(enabled=True, speed='1000', attached=True))
    args.update(changes)
    return check.check_port(5, **args)


class VerdictTests(unittest.TestCase):
    def test_a_working_port_is_ok_and_carries_the_measurements(self):
        r = run()
        self.assertEqual((r['verdict'], r['remedy'], r['expected_interface']), ('ok', None, 'BCM_PORT_IF_GMII'))
        self.assertEqual((r['diagnostics']['tx_power_dbm'], r['link']['link_mode'], r['interface']), (-5.2, '1000BASE-X', 'ethernet1/5'))

    def test_first_failing_layer_wins_in_packet_order(self):
        cases = [
            (dict(cage=dict(present=False, tx_disable=True)), 'no-module', None),
            (dict(cage=None), 'no-module', None),
            (dict(committed=dict(enabled=False, speed='auto', attached=True)), 'disabled', None),
            (dict(cage=dict(present=True, tx_disable=True)), 'transmitter-off', 'enable-transmitter'),
            (dict(diagnostics=dict(GOOD, tx_disable=True)), 'transmitter-off', 'enable-transmitter'),
            (dict(diagnostics=dict(GOOD, tx_fault=True)), 'tx-fault', 'hardware'),
            (dict(diagnostics=dict(GOOD, tx_power_dbm=-31.0)), 'laser-off', 'hardware'),
            (dict(diagnostics=dict(GOOD, tx_power_dbm=None)), 'laser-off', 'hardware'),
            (dict(diagnostics=dict(GOOD, rx_los=True)), 'no-rx-light', 'far-end'),
            (dict(diagnostics=dict(GOOD, rx_power_dbm=-35.0)), 'no-rx-light', 'far-end'),
            (dict(link=dict(LINK, interface='BCM_PORT_IF_SGMII')), 'link-mode-mismatch', 'relink'),
            (dict(link=dict(LINK, mac_enabled=False)), 'link-down', 'far-end'),
            (dict(link=dict(LINK, link=False)), 'link-down', 'far-end'),
            (dict(attached=dict(ATTACHED, state='pending', pending='start')), 'attachment-pending', 'reapply'),
            (dict(attached=dict(ATTACHED, state='stale', epoch='old')), 'attachment-stale', 'reapply'),
            (dict(attached=dict(state='none', enabled=False, pending=None, epoch=None)), 'attachment-off', 'reapply'),
        ]
        for changes, verdict, remedy in cases:
            with self.subTest(verdict=verdict):
                r = run(**changes)
                self.assertEqual((r['verdict'], r['remedy']), (verdict, remedy))
                self.assertTrue(r['detail'])

    def test_diagnostics_absent_or_unreadable_still_judge_the_other_layers(self):
        r = run(diagnostics=None)
        self.assertEqual(r['verdict'], 'ok')
        r = run(diagnostics=dict(error='[Errno 145] Connection timed out'))
        self.assertEqual(r['verdict'], 'ok'); self.assertIn('unreadable', r['detail'])
        r = run(diagnostics=dict(diagnostics_available=False), link=dict(LINK, link=False))
        self.assertEqual(r['verdict'], 'link-down')
        r = run(diagnostics=dict(error='bus'), cage=dict(present=True, tx_disable=True))
        self.assertEqual(r['verdict'], 'transmitter-off')

    def test_expected_interface_follows_the_module_and_the_committed_speed(self):
        self.assertEqual(check.expected_interface(MODULE_1G, 'auto'), 'BCM_PORT_IF_GMII')
        self.assertEqual(check.expected_interface(MODULE_10G, 'auto'), 'BCM_PORT_IF_XFI')
        dual = dict(MODULE_10G, speeds=[1000, 10000])
        self.assertEqual(check.expected_interface(dual, '1000'), 'BCM_PORT_IF_GMII')
        self.assertEqual(check.expected_interface(dual, 'auto'), 'BCM_PORT_IF_XFI')
        self.assertIsNone(check.expected_interface(dual, '2500'))
        self.assertIsNone(check.expected_interface(None, 'auto'))
        self.assertIsNone(check.expected_interface(dict(speeds=[]), 'auto'))
        r = run(module=MODULE_10G, link=dict(LINK, interface='BCM_PORT_IF_XFI'), committed=dict(enabled=True, speed='auto', attached=True))
        self.assertEqual(r['verdict'], 'ok')
        r = run(module=None)   # unknown module: the link mode cannot be judged, the rest can
        self.assertEqual((r['verdict'], r['expected_interface']), ('ok', None))

    def test_attachment_journal_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp)
            self.assertEqual(check.attachment(5, journal, 'e')['state'], 'none')
            (journal / 'physical-5.json').write_text(json.dumps(dict(port=5, epoch='e', enabled=True, pending=None)))
            self.assertEqual(check.attachment(5, journal, 'e')['state'], 'enabled')
            self.assertEqual(check.attachment(5, journal, 'other')['state'], 'stale')
            self.assertEqual(check.attachment(5, journal, None)['state'], 'enabled')   # no live epoch: cannot call it stale
            (journal / 'physical-5.json').write_text(json.dumps(dict(port=5, epoch='e', enabled=False, pending='start')))
            self.assertEqual(check.attachment(5, journal, 'e')['state'], 'pending')
            (journal / 'physical-5.json').write_text(json.dumps(dict(port=5, epoch='e', enabled=False, pending=None)))
            self.assertEqual(check.attachment(5, journal, 'e')['state'], 'disabled')
            (journal / 'physical-5.json').write_text('garbage')
            self.assertEqual(check.attachment(5, journal, 'e')['state'], 'none')


class CollectTests(unittest.TestCase):
    def test_collect_walks_every_cage_rate_limiting_diagnostics_to_present_modules(self):
        reads = []
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp)
            (journal / 'physical-5.json').write_text(json.dumps(dict(port=5, epoch='e', enabled=True, pending=None)))
            sources = dict(
                inventory=lambda: {5: dict(present=True, tx_disable=False, tx_enabled=True), 6: dict(present=False, tx_disable=True, tx_enabled=False)},
                faceplate=lambda: dict(ports=[dict(port=5, enabled=True, mac_enabled=True, link=True, interface='BCM_PORT_IF_GMII', link_mode='1000BASE-X', autoneg=True, configured_speed='1000'),
                                              dict(port=6, enabled=False, mac_enabled=False, link=False, interface=None, link_mode=None, autoneg=False, configured_speed='auto')],
                                       saved=dict(ports={'5': True}, speeds={'5': '1000'})),
                module=lambda port, present: MODULE_1G if present else None,
                diagnostics=lambda port: reads.append(port) or dict(GOOD),
                epoch=lambda: 'e', journal=journal, committed_attachment=lambda port: True)
            results = check.collect((5, 6), diagnostics=True, sources=sources)
            self.assertEqual([(r['port'], r['verdict']) for r in results], [(5, 'ok'), (6, 'no-module')])
            self.assertEqual(reads, [5])
            self.assertEqual(check.summary(results), {'no-module': 1, 'ok': 1})
            sources['diagnostics'] = lambda port: (_ for _ in ()).throw(OSError('[Errno 145] Connection timed out'))
            results = check.collect((5,), diagnostics=True, sources=sources)
            self.assertEqual(results[0]['verdict'], 'ok'); self.assertIn('unreadable', results[0]['detail'])
            self.assertEqual(check.collect((5,), diagnostics=False, sources=sources)[0]['diagnostics'], None)
            published = check.publish(results, path=journal / 'health.json', clock=lambda: 1.0)
            self.assertEqual((published['schema'], published['checked_at'], len(published['ports'])), (1, 1.0, 1))
            self.assertEqual(json.loads((journal / 'health.json').read_text())['ports'][0]['port'], 5)


if __name__ == '__main__':
    unittest.main()
