import json
from pathlib import Path
import tempfile
import unittest
from ffn_network_init import initialize


class InitializationTests(unittest.TestCase):
    def test_only_missing_state_gets_disabled_ports(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'network.json'
            result = initialize(path, 4)
            self.assertEqual(result['ports'], {'p%d' % i: {'mode': 'disabled'} for i in range(1, 5)})
            self.assertEqual(json.loads(path.read_text()), result)
            self.assertEqual(result['routes'], [])

    def test_preserves_existing_configuration_and_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'network.json'
            original = '{"revision":7,"ports":{"p1":{"mode":"l3","addresses":["192.0.2.7/24"]}}}'
            path.write_text(original)
            self.assertEqual(initialize(path)['revision'], 7)
            self.assertEqual(path.read_text(), original)

    def test_invalid_existing_state_is_not_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'network.json'
            path.write_text('{invalid')
            with self.assertRaises(ValueError):
                initialize(path)
            self.assertEqual(path.read_text(), '{invalid')


if __name__ == '__main__':
    unittest.main()
