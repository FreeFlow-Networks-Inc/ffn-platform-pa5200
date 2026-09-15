import copy
import unittest
from unittest.mock import patch
import ffn_lacp as lacp


def profile():
    return {'revision': 0, 'groups': {'lag1': {'members': ['p1', 'p3'],
        'activity': 'active', 'rate': 'fast', 'hash': 'layer2+3', 'min_links': 1,
        'system_priority': 32768, 'network': {'mode': 'l3', 'addresses': ['192.0.2.1/24']}}}}


class LACPTests(unittest.TestCase):
    def test_filtered_iproute_empty_records(self):
        with patch.object(lacp.net,'exists',return_value=True), patch.object(lacp.net,'ip',return_value='[{},{}]'):
            self.assertEqual(lacp.runtime(),{})
    def test_empty_install_and_valid_profile(self):
        lacp.validate({'revision': 0, 'groups': {}})
        lacp.validate(profile())

    def test_duplicate_and_overlapping_members_rejected(self):
        for members in (['p1','p1'], ['p0','p3'], ['p25','p3'], ['p1']):
            cfg = profile(); cfg['groups']['lag1']['members'] = members
            with self.assertRaises(ValueError): lacp.validate(cfg)
        cfg = profile(); cfg['groups']['lag2'] = copy.deepcopy(cfg['groups']['lag1'])
        with self.assertRaises(ValueError): lacp.validate(cfg)

    def test_invalid_protocol_options(self):
        for field, value in [('rate','turbo'), ('activity','on'), ('min_links',True),
                             ('min_links',3), ('system_priority',0), ('hash','round-robin')]:
            cfg = profile(); cfg['groups']['lag1'][field] = value
            with self.assertRaises(ValueError): lacp.validate(cfg)

    def test_stale_revision_never_changes_kernel(self):
        with patch.object(lacp, 'runtime') as live, patch.object(lacp, 'save') as save:
            with self.assertRaises(ValueError): lacp.change(profile(), 'set', {'revision':1,'groups':{}})
            live.assert_not_called(); save.assert_not_called()

    def test_save_does_not_activate(self):
        with patch.object(lacp, 'runtime', return_value={}), patch.object(lacp, 'save') as save, patch.object(lacp, 'activate') as activate:
            lacp.change({'revision':0,'groups':{}}, 'set', profile())
            self.assertEqual(save.call_args.args[0]['revision'], 1)
            activate.assert_not_called()

    def test_active_group_cannot_be_deleted(self):
        with patch.object(lacp, 'runtime', return_value={'lag1':{}}), patch.object(lacp, 'save') as save:
            with self.assertRaises(ValueError): lacp.change(profile(), 'set', {'revision':0,'groups':{}})
            save.assert_not_called()

    def test_relay_activation_rejected_before_kernel_writes(self):
        with patch.object(lacp.net, 'backend', return_value={'transport':'MP SSH relay'}), patch.object(lacp.net, 'ip') as ip:
            with self.assertRaises(ValueError): lacp.activate('lag1', profile()['groups']['lag1'])
            ip.assert_not_called()

    def test_partial_activation_restores_member_and_deletes_bond(self):
        links = {p:{'address':'02:00:00:00:00:01','mtu':1500} for p in ['p1','p3']}
        calls = []
        def ip(*args):
            calls.append(args)
            if args == ('link','set','p3','master','lag1'): raise RuntimeError('injected failure')
        with patch.object(lacp,'preflight',return_value=links), patch.object(lacp.net,'ip',side_effect=ip):
            with self.assertRaisesRegex(RuntimeError, 'cleanup errors: \[\]'):
                lacp.activate('lag1',profile()['groups']['lag1'])
        self.assertIn(('link','set','p1','down'), calls)
        self.assertIn(('link','set','p3','nomaster'), calls)
        self.assertEqual(calls[-1], ('link','delete','lag1'))


if __name__ == '__main__': unittest.main()
