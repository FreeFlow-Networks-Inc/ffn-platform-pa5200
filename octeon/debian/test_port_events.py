import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
import ffn_port_events as events


class PortEventsTest(unittest.TestCase):
    def inventory(self, enabled=True, link=True):
        return {'ok': True, 'ports': [{'port': p, 'enabled': enabled,
                'link': link, 'speed_mb': 10000} for p in events.MAP]}

    def test_all_front_ports_and_disabled_carrier(self):
        rows = events.normalize(self.inventory(False))
        self.assertEqual(len({p['bcm_port'] for p in rows}), 24)
        self.assertEqual([p['port'] for p in rows], list(range(1,25)))
        self.assertTrue(all(p['carrier'] is False for p in rows))
        self.assertFalse(set(events.MAP) & {3,24,25,26})

    def test_missing_port_unknown(self):
        source = self.inventory(); source['ports'].pop(0)
        self.assertIsNone(events.normalize(source)[0]['carrier'])

    def test_reject_duplicate_and_invalid_observation(self):
        source = self.inventory(); source['ports'].append(source['ports'][0])
        with self.assertRaises(ValueError): events.normalize(source)
        for field, value in [('enabled', 1), ('link', 'up'), ('speed_mb', 0)]:
            source = self.inventory(); source['ports'][0][field] = value
            with self.assertRaises(ValueError): events.normalize(source)

    def test_sequence_failure_recovery(self):
        ports = events.normalize(self.inventory())
        a = events.sample(None, ports, None, 'generation', 'boot', 10)
        b = events.sample(a, ports, None, 'generation', 'boot', 12)
        self.assertEqual(b['sequence'], 1)
        self.assertEqual(b['changed_ports'], [])
        c = events.sample(b, events.unknown(), 'unavailable', 'generation', 'boot', 14)
        d = events.sample(c, ports, None, 'generation', 'boot', 16)
        self.assertEqual(d['sequence'], 3)
        self.assertEqual(len(d['changed_ports']), 24)

    def test_stale_reboot_and_clock_regression(self):
        state = events.sample(None, events.normalize(self.inventory()), None, 'g', 'boot', 10)
        for boot, now in [('new-boot', 11), ('boot', 17), ('boot', 9)]:
            result = events.qualify(state, boot, now)
            self.assertTrue(result['stale'])
            self.assertTrue(all(p['carrier'] is None for p in result['ports']))
        self.assertFalse(events.qualify(state, 'boot', 12)['stale'])

    def test_hardware_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'vendor').write_text('0x14e4\n')
            (root/'device').write_text('0x8375\n')
            events.require_cp(root)
            (root/'device').write_text('0xffff\n')
            with self.assertRaises(ValueError): events.require_cp(root)

    def test_query_rejects_truncated_and_oversized_input(self):
        for data in [b'{}', b'x' * (events.MAX_RESPONSE + 1)]:
            with patch.object(events.socket, 'create_connection') as connect:
                sock = connect.return_value.__enter__.return_value
                chunks = [data[i:i+4096] for i in range(0, len(data), 4096)]
                sock.recv.side_effect = chunks + [b'']
                with self.assertRaises(ValueError): events.query()

    def test_query_total_deadline(self):
        with patch.object(events.socket, 'create_connection') as connect:
            sock = connect.return_value.__enter__.return_value
            sock.recv.return_value = b' '
            with patch.object(events.time, 'monotonic', side_effect=[0,0,1,3]):
                with self.assertRaises(TimeoutError): events.query()


if __name__ == '__main__': unittest.main()
