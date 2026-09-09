import copy
import unittest
from unittest.mock import patch
import ffn_network as net


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {'revision': 7, 'ports': {
            'p1': {'mode': 'l2', 'vlans': [100], 'pvid': 100},
            'p3': {'mode': 'l3', 'addresses': ['198.18.1.1/24']}}}

    def test_reject_invalid_before_io(self):
        for settings in ({'mode': 'l2', 'vlans': [0]},
                         {'mode': 'l2', 'vlans': [100], 'pvid': 200},
                         {'mode': 'l2', 'vlans': [100], 'addresses': ['198.18.1.1/24']},
                         {'mode': 'l3', 'addresses': ['bad']},
                         {'mode': 'disabled', 'mtu': 12}):
            with self.subTest(settings=settings), self.assertRaises(ValueError), patch.object(net, 'run') as io:
                net.patch(self.cfg, {'revision': 7, 'ports': {'p1': settings}})
            io.assert_not_called()

    def test_revision_conflict(self):
        with patch.object(net, 'run') as io, self.assertRaises(ValueError):
            net.patch(self.cfg, {'revision': 6, 'ports': {'p1': {'mode': 'disabled'}}})
        io.assert_not_called()

    def test_only_changed_port(self):
        original = copy.deepcopy(self.cfg)
        with patch.object(net, 'exists', return_value=True), patch.object(net, 'configure_port') as change, patch.object(net, 'save'):
            result = net.patch(self.cfg, {'revision': 7, 'ports': {'p1': {'mode': 'disabled'}}})
        change.assert_called_once_with('p1', {'mode': 'disabled'}, create=False)
        self.assertEqual(result['revision'], 8)
        self.assertEqual(original, self.cfg)

    def test_persistence_failure_restores_port(self):
        with patch.object(net, 'exists', return_value=True), patch.object(net, 'configure_port') as change, patch.object(net, 'save', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(RuntimeError, 'rollback errors: \\[\\]'):
                net.patch(self.cfg, {'revision': 7, 'ports': {'p1': {'mode': 'disabled'}}})
        self.assertEqual(change.call_args_list[-1].args, ('p1', self.cfg['ports']['p1']))


if __name__ == '__main__':
    unittest.main()
