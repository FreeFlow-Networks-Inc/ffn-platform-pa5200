import unittest
from ffn_fe100_offload_policy import (admissible, should_offload, MAX_HARDWARE_SESSIONS, MIN_AGE_SECONDS,
                                      MIN_PACKETS_EACH_WAY, MIN_BYTES, SCARCITY_FRACTION, SCARCE_MIN_BYTES, FAILURE_BACKOFF)


def row(**extra):
    base = dict(proto=6, state=3, candidate=True, l3={'available': True, 'pair': ['ae1.69', 'ethernet1/5']},
                packets={'original': 400, 'reply': 900}, bytes={'original': 30000, 'reply': 1200000},
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
    """Invariants any implementation of should_offload must keep."""
    def decide(self, r, **state):
        s = dict(in_hardware=0, slots=MAX_HARDWARE_SESSIONS, now=160.0, recent_failures=0)
        s.update(state)
        return should_offload(r, s)

    def test_returns_a_boolean(self):
        self.assertIn(self.decide(row()), (True, False))

    def test_full_table_admits_nothing(self):
        self.assertFalse(self.decide(row(), in_hardware=MAX_HARDWARE_SESSIONS))
        self.assertFalse(self.decide(row(), in_hardware=MAX_HARDWARE_SESSIONS + 5))
        self.assertFalse(self.decide(row(), slots=0))

    def test_a_long_fat_established_flow_is_admitted_with_room(self):
        self.assertTrue(self.decide(row(packets={'original': 5000, 'reply': 12000}, since=100.0), now=170.0))


class DefaultPolicyTests(unittest.TestCase):
    """The default policy: long-lived, two-way, fat or fast flows; bounded slots; back-off; a higher bar when scarce."""
    def decide(self, r, **state):
        s = dict(in_hardware=0, slots=MAX_HARDWARE_SESSIONS, now=160.0, recent_failures=0)
        s.update(state)
        return should_offload(r, s)

    def test_the_fixture_flow_qualifies_by_volume(self):
        # 60 s old, 1.2 MB, 400/900 packets: a download that has settled in.
        self.assertTrue(self.decide(row()))

    def test_too_young_waits(self):
        self.assertFalse(self.decide(row(since=160.0 - MIN_AGE_SECONDS / 2)))
        self.assertTrue(self.decide(row(since=160.0 - MIN_AGE_SECONDS - 1, bytes={'original': 0, 'reply': MIN_BYTES})))

    def test_one_way_traffic_is_never_carried(self):
        self.assertFalse(self.decide(row(proto=17, state=None, packets={'original': 5000, 'reply': 0}, bytes={'original': 50 << 20, 'reply': 0})))
        self.assertFalse(self.decide(row(packets={'original': MIN_PACKETS_EACH_WAY - 1, 'reply': 5000})))

    def test_thin_but_old_flows_stay_in_software(self):
        # An idle ssh session: old, two-way, but a few kilobytes and a few hundred bits per second.
        self.assertFalse(self.decide(row(since=0.0, packets={'original': 300, 'reply': 300}, bytes={'original': 20000, 'reply': 30000}), now=600.0))

    def test_a_fast_young_stream_qualifies_by_rate(self):
        # 6 s old, 600 kB so far = 800 kbit/s averaged: a stream still ramping up.
        self.assertTrue(self.decide(row(since=154.0, packets={'original': 200, 'reply': 450}, bytes={'original': 20000, 'reply': 580000})))

    def test_failures_back_off_this_pass(self):
        self.assertFalse(self.decide(row(), recent_failures=FAILURE_BACKOFF))
        self.assertTrue(self.decide(row(), recent_failures=FAILURE_BACKOFF - 1))

    def test_scarcity_raises_the_bar(self):
        scarce = int(MAX_HARDWARE_SESSIONS * SCARCITY_FRACTION)
        medium = row(bytes={'original': 100000, 'reply': 2 << 20})
        self.assertTrue(self.decide(medium, in_hardware=scarce - 1))
        self.assertFalse(self.decide(medium, in_hardware=scarce))
        fat = row(bytes={'original': 1 << 20, 'reply': SCARCE_MIN_BYTES})
        self.assertTrue(self.decide(fat, in_hardware=scarce))
        self.assertFalse(self.decide(fat, in_hardware=MAX_HARDWARE_SESSIONS))

    def test_malformed_rows_are_not_admitted(self):
        self.assertFalse(self.decide(row(packets=None)))
        self.assertFalse(self.decide(row(bytes='lots')))
        self.assertFalse(self.decide(row(since=None)))
        self.assertFalse(self.decide(row(since='100')))
        self.assertFalse(self.decide(row(packets={'original': True, 'reply': 900})))
        self.assertFalse(self.decide(row(), now=None))
        self.assertFalse(self.decide(row(), slots=None))


if __name__ == '__main__':
    unittest.main()
