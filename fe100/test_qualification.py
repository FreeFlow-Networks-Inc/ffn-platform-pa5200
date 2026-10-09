import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
import ffn_fe100_qualification as q

BOOT = 'aaaaaaaa-1111-4111-8111-111111111111'
EPOCH = BOOT + ':4242:1234'
OWNERS = {'ae1': {'token': 't' * 8, 'ports': [21, 22]}}
LIFETIME = dict(cp_boot_id=BOOT, bcm_epoch=EPOCH)


def case(protocol, translation, ingress, internal=False, reverse=False, boot=BOOT, epoch=EPOCH):
    return dict(protocol=protocol, translation=translation, ingress=ingress, egress=ingress if internal else 18 - ingress,
                packets=1 if internal else 4, internal_mac_loopback=internal, source_mac_rewrite=internal and protocol == 'tcp',
                nat_reverse=reverse, cp_boot_id=boot, bcm_epoch=epoch, production_owners=OWNERS)


def external(**changes):
    cases = [case(p, t, i) for p, t, i in sorted(q.EXTERNAL_CASES)]
    value = dict(schema=1, scope='isolated IPv4 NAT packet rewrite; sequential directional tests', complete=True, cases=cases,
                 missing=[], production_admission=False, tcp_state_tracking_verified=False,
                 simultaneous_bidirectional_verified=False, aggregate_transit_verified=False,
                 sources=[dict(path='/var/log/r.json', sha256='0' * 64)])
    value.update(changes)
    return value


def internal(**changes):
    cases = [case(p, 'port', 7, internal=True, reverse=r) for p, _, r in sorted(q.INTERNAL_CASES)]
    value = dict(schema=1, scope='internal MAC NAT rewrite probes', cases=cases, production_admission=False,
                 external_wire_verified=False, tcp_state_tracking_verified=False, sources=[])
    value.update(changes)
    return value


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'run' / 'qualification.json'
        self.lifetime = dict(LIFETIME); self.now = 5000.0

    def qualification(self, lifetime=None):
        return q.Qualification(self.path, lifetime or (lambda: dict(self.lifetime)), clock=lambda: self.now)

    def test_external_matrix_is_recorded_and_lifts_both_gates_for_this_lifetime(self):
        space = self.qualification()
        self.assertEqual(space.current()['reason'], 'no qualification record for this CP boot')
        self.assertFalse(space.front_port()); self.assertFalse(space.nat())
        state = space.record(external(), 'external-wire')
        self.assertEqual((state['front_port'], state['nat_rewrite'], state['scope'], state['cases'], state['recorded_at'], state['reason']),
                         (True, True, 'external-wire', 8, 5000.0, None))
        self.assertTrue(space.front_port()); self.assertTrue(space.nat())
        self.assertEqual(space.health(), dict(nat_offload_verified=True))
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        row = json.loads(self.path.read_text())
        self.assertEqual((row['cp_boot_id'], row['bcm_epoch'], row['production_owners'], len(row['sources'])), (BOOT, EPOCH, OWNERS, 1))
        self.assertEqual(set(row['cases'][0]), set(q.KEPT))

    def test_external_scope_requires_the_complete_wire_matrix(self):
        for summary, reason in [(external(complete=False), 'complete matrix'),
                                (external(cases=external()['cases'][:7]), 'eight audited cases'),
                                (external(cases=[case('udp', 'port', 5, internal=True)] + external()['cases'][1:]), 'wire cases only'),
                                (external(production_admission=True), 'never authorises'),
                                (external(schema=2), 'schema 1'), (external(cases=[]), 'no audited cases'),
                                (external(cases=[dict(c, extra=1) for c in external()['cases']]), 'fields differ')]:
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                q.accept(summary, 'external-wire', LIFETIME)
        with self.assertRaisesRegex(ValueError, 'scope must be one of'):
            q.accept(external(), 'bench', LIFETIME)

    def test_internal_scope_requires_both_protocols_and_directions_on_the_loop(self):
        space = self.qualification()
        state = space.record(internal(), 'internal-loop')
        self.assertEqual((state['front_port'], state['nat_rewrite'], state['scope'], state['cases']), (True, True, 'internal-loop', 4))
        short = internal(); short['cases'] = [c for c in short['cases'] if not (c['protocol'] == 'udp' and c['nat_reverse'])]
        with self.assertRaisesRegex(ValueError, 'missing: udp port reverse'):
            q.accept(short, 'internal-loop', LIFETIME)
        with self.assertRaisesRegex(ValueError, 'internal MAC loop cases only'):
            q.accept(internal(external_wire_verified=True), 'internal-loop', LIFETIME)
        with self.assertRaisesRegex(ValueError, 'internal MAC loop cases only'):
            q.accept(external(), 'internal-loop', LIFETIME)

    def test_cases_from_another_lifetime_are_refused_and_a_changed_lifetime_retires_the_record(self):
        other = external(); other['cases'][3] = case('tcp', 'address', 5, epoch=BOOT + ':9:9')
        with self.assertRaisesRegex(ValueError, 'cross hardware'):
            q.accept(other, 'external-wire', LIFETIME)
        with self.assertRaisesRegex(ValueError, 'another CP boot or BCM owner'):
            q.accept(external(), 'external-wire', dict(cp_boot_id=BOOT, bcm_epoch=BOOT + ':9:9'))
        space = self.qualification(); space.record(external(), 'external-wire')
        self.lifetime['bcm_epoch'] = BOOT + ':4243:5678'   # the switch daemon restarted
        state = space.current()
        self.assertEqual((state['front_port'], state['nat_rewrite'], state['scope']), (False, False, 'external-wire'))
        self.assertIn('another CP boot or BCM owner', state['reason'])
        self.assertFalse(space.front_port()); self.assertEqual(space.health(), dict(nat_offload_verified=False))

    def test_unavailable_lifetime_and_damaged_records_never_qualify(self):
        def broken():
            raise RuntimeError('one BCM owner process required')
        space = self.qualification(); space.record(external(), 'external-wire')
        state = q.Qualification(self.path, broken).current()
        self.assertFalse(state['front_port']); self.assertIn('one BCM owner process required', state['reason'])
        self.path.write_text('{"schema": 1, "scope": "bench"}')
        self.assertEqual(space.current()['reason'], 'no qualification record for this CP boot')
        self.path.write_text('not json')
        self.assertFalse(space.front_port())

    def test_a_new_record_replaces_the_view_at_once(self):
        space = self.qualification(); space.record(external(), 'external-wire')
        self.now = 6000.0; space.record(internal(), 'internal-loop')
        self.assertEqual((space.current()['scope'], space.current()['recorded_at']), ('internal-loop', 6000.0))
        self.path.unlink()
        self.assertEqual(space.current()['reason'], 'no qualification record for this CP boot')


if __name__ == '__main__':
    unittest.main()
