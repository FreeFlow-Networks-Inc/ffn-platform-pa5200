import json
import tempfile
import unittest
from pathlib import Path
import ffn_fe100_return as ret

FRONT = {5: 16, 21: 32, 22: 33, 13: 7}
CONFIG = {'schema': 1, 'trunk': 24, 'return_port': 20, 'lease_ms': 3000,
          'ports': {'5': {'in_lif': 5, 'zone': 10}, '21': {'in_lif': 21, 'zone': 20}, '22': {'in_lif': 22, 'zone': 20}}}


class Owner:
    def __init__(self): self.calls = []
    def fe100(self, rows, deadline): self.calls.append((rows, deadline))


class LoadTests(unittest.TestCase):
    def write(self, value):
        path = Path(tempfile.mkdtemp()) / 'fe100-return.json'
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        return path

    def test_valid_file_loads(self):
        self.assertEqual(ret.load(self.write(CONFIG)), CONFIG)

    def test_absent_empty_or_invalid_is_nothing_to_bind(self):
        self.assertIsNone(ret.load(Path(tempfile.mkdtemp()) / 'missing.json'))
        self.assertIsNone(ret.load(self.write('')))
        self.assertIsNone(ret.load(self.write('{not json')))
        for bad in (dict(CONFIG, schema=2), dict(CONFIG, trunk=20), dict(CONFIG, lease_ms=50),
                    dict(CONFIG, ports={'0': {'in_lif': 1, 'zone': 1}}), dict(CONFIG, ports={'5': {'in_lif': 5}}),
                    dict(CONFIG, ports={str(p): {'in_lif': p, 'zone': 1} for p in range(1, 10)})):
            self.assertIsNone(ret.load(self.write(bad)), bad)


class BindingTests(unittest.TestCase):
    def test_only_the_owners_ports_with_their_system_ports(self):
        rows = ret.bindings(CONFIG, [21, 22], FRONT)
        self.assertEqual(rows, [dict(trunk=24, return_port=20, front=21, in_lif=21, zone=20, source=32),
                                dict(trunk=24, return_port=20, front=22, in_lif=22, zone=20, source=33)])
        self.assertEqual(ret.bindings(CONFIG, [13], FRONT), [])          # not commissioned
        self.assertEqual(ret.bindings(None, [5], FRONT), [])
        self.assertEqual(ret.bindings(CONFIG, [5], {}), [])              # no system port known

    def test_renew_leases_every_tick_and_revokes_once(self):
        owner = Owner()
        state = ret.renew(owner, CONFIG, [5], FRONT, 1000, None)
        self.assertEqual(state['deadline_ms'], 4000)
        state = ret.renew(owner, CONFIG, [5], FRONT, 2000, state)
        self.assertEqual(state['deadline_ms'], 5000)
        self.assertEqual([d for _, d in owner.calls], [4000, 5000])
        state = ret.renew(owner, None, [5], FRONT, 3000, state)        # file gone: revoke once
        self.assertIsNone(state)
        self.assertEqual(owner.calls[-1], ([], 0))
        state = ret.renew(owner, None, [5], FRONT, 4000, state)        # and then nothing
        self.assertEqual(len(owner.calls), 3)


if __name__ == '__main__':
    unittest.main()
