import unittest
from ffn_fe100_offload_policy import admissible, should_offload, MAX_HARDWARE_SESSIONS


def row(**extra):
    base = dict(proto=6, state=3, candidate=True, l3={'available': True, 'pair': ['ae1.69', 'ethernet1/5']},
                packets={'original': 400, 'reply': 900}, bytes={'original': 30000, 'bytes': 1200000},
                since=100.0, rule='LAN-TO-WAN', dport=443, xdport=443)
    base.update(extra)
    return base


class GateTests(unittest.TestCase):
    """The non-negotiable gate the admission loop applies before asking the policy."""
    def test_established_tcp_candidate_with_plan_is_admissible(self):
        self.assertTrue(admissible(row()))

    def test_udp_candidate_is_admissible(self):
        self.assertTrue(admissible(row(proto=17, state=None)))

    def test_blocked_rows_never_reach_the_policy(self):
        self.assertFalse(admissible(row(candidate=False)))
        self.assertFalse(admissible(row(l3={'available': False})))
        self.assertFalse(admissible(row(proto=1)))
        self.assertFalse(admissible(row(state=4)))          # closing TCP
        self.assertFalse(admissible(row(state=1)))          # handshake


class PolicyTests(unittest.TestCase):
    """Invariants any implementation of should_offload must keep; skipped until it exists."""
    def decide(self, r, **state):
        s = dict(in_hardware=0, slots=MAX_HARDWARE_SESSIONS, now=160.0, recent_failures=0)
        s.update(state)
        try:
            return should_offload(r, s)
        except NotImplementedError:
            self.skipTest('offload policy not implemented yet')

    def test_returns_a_boolean(self):
        self.assertIn(self.decide(row()), (True, False))

    def test_full_table_admits_nothing(self):
        self.assertFalse(self.decide(row(), in_hardware=MAX_HARDWARE_SESSIONS))

    def test_a_long_fat_established_flow_is_admitted_with_room(self):
        self.assertTrue(self.decide(row(packets={'original': 5000, 'reply': 12000}, since=100.0), now=170.0))


if __name__ == '__main__':
    unittest.main()
