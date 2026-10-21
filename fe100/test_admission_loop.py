import unittest
from ffn_fe100_admission_loop import AdmissionLoop, compact, IDLE_TIMEOUT, MAXIMUM_LIFETIME, FAILURE_RETRY
from ffn_fe100_offload_policy import MIN_PACKETS_EACH_WAY, MIN_BYTES, FAILURE_BACKOFF


def stream_row(identity, *, proto=6, state=3, candidate=True, packets=(400, 900), octets=(30000, 1 << 21), l3=True, ct=1000):
    return dict(identity=identity, conntrack_id=ct, software_candidate=candidate, blockers=[] if candidate else ['x'],
                original=dict(source='10.1.0.63', destination='151.101.2.132', source_port=50000, destination_port=443, protocol=proto),
                reply=dict(source='151.101.2.132', destination='152.193.185.10', source_port=443, destination_port=50000, protocol=proto),
                translated=dict(destination='151.101.2.132', destination_port=443), tcp_state=state,
                counters=dict(original=dict(packets=packets[0], bytes=octets[0]), reply=dict(packets=packets[1], bytes=octets[1])),
                rule=dict(scope='vsys1/security', name='LAN-TO-WAN', interface_pairs=[['ae1.69', 'ethernet1/5']], inspection_required=False),
                nat=dict(source=True), l3=dict(available=l3, pair=['ae1.69', 'ethernet1/5'], directions=[]), token='t')


class Receiver:
    def __init__(self):
        self.ready = True; self.sessions = {}; self.since = {}
        self.producer = dict(boot_id='1b1582dd-a310-4cc1-85cd-0be7e600ae62', pid=41608, process_start='276956',
                             stream_id='0a4fb0e0-0000-4000-8000-000000000001')
        self.policy = dict(bindings={'ae1.69': {'index': 67}, 'ethernet1/5': {'index': 5}}, nat_digest='b' * 64)

    def put(self, row, at): self.sessions[row['identity']] = row; self.since.setdefault(row['identity'], at)
    def drop(self, identity): self.sessions.pop(identity, None); self.since.pop(identity, None)


class Clock:
    def __init__(self, at=1000.0): self.at = at
    def __call__(self): return self.at


class CompactTests(unittest.TestCase):
    def test_stream_row_maps_to_the_policy_row(self):
        c = compact(stream_row('a'), 100.0)
        self.assertEqual((c['proto'], c['state'], c['candidate'], c['since'], c['rule'], c['dport'], c['xdport']), (6, 3, True, 100.0, 'LAN-TO-WAN', 443, 443))
        self.assertEqual(c['packets'], dict(original=400, reply=900)); self.assertEqual(c['bytes'], dict(original=30000, reply=1 << 21))
        self.assertTrue(c['l3']['available'])

    def test_missing_and_malformed_fields_read_as_absent(self):
        c = compact(dict(identity='a', counters='no', original=None, rule=[], l3=None), 5.0)
        self.assertEqual(c['packets'], dict(original=0, reply=0)); self.assertIsNone(c['proto']); self.assertFalse(c['candidate']); self.assertEqual(c['l3'], {})


class ObserveModeTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(); self.receiver = Receiver()
        self.loop = AdmissionLoop('observe', clock=self.clock, slots=4)

    def test_mature_two_way_rows_get_shadow_leases_and_thin_or_young_ones_do_not(self):
        self.receiver.put(stream_row('fat'), 900.0)                                   # 100 s old, 2 MB
        self.receiver.put(stream_row('young'), 999.0)                                 # 1 s old
        self.receiver.put(stream_row('thin', packets=(20, 30), octets=(2000, 3000)), 0.0)
        self.receiver.put(stream_row('oneway', proto=17, state=None, packets=(5000, 0), octets=(9 << 20, 0)), 0.0)
        self.receiver.put(stream_row('blocked', candidate=False), 0.0)
        status = self.loop.run(self.receiver)
        self.assertEqual(status['leases'], 1); self.assertIn('fat', self.loop.leases)
        self.assertEqual(status['last_pass']['reasons'], {'not admissible': 1, 'policy: not now': 3, 'admitted': 1})
        self.assertFalse(status['hardware_admission'])

    def test_leases_follow_the_row_renewed_on_traffic_idle_without_it_closed_when_gone(self):
        row = stream_row('s'); self.receiver.put(row, 900.0)
        self.loop.run(self.receiver); self.assertEqual(self.loop.leases['s']['expires'], 1000.0 + IDLE_TIMEOUT)
        self.clock.at = 1030.0; row['counters']['reply']['packets'] += 10; self.loop.run(self.receiver)
        self.assertEqual(self.loop.leases['s']['expires'], 1030.0 + IDLE_TIMEOUT); self.assertEqual(self.loop.counters['renewed'], 1)
        self.clock.at = 1030.0 + IDLE_TIMEOUT; status = self.loop.run(self.receiver)
        self.assertNotIn('s', self.loop.leases); self.assertEqual(self.loop.counters['closed:idle'], 1)
        self.assertEqual(status['last_pass']['reasons'], {'idle since withdrawal': 1}, 'an idle row is not re-admitted on its lifetime volume')
        self.clock.at += 1; self.loop.run(self.receiver); self.assertNotIn('s', self.loop.leases)
        row['counters']['original']['packets'] += 1; self.loop.run(self.receiver)
        self.assertIn('s', self.loop.leases, 'a row that moves again is admitted again')
        self.receiver.drop('s'); self.clock.at += 1; self.loop.run(self.receiver)
        self.assertEqual(self.loop.counters['closed:row closed'], 1); self.assertEqual(self.loop.leases, {}); self.assertEqual(self.loop.quiet, {})

    def test_the_hard_lifetime_withdraws_a_busy_lease(self):
        row = stream_row('s'); self.receiver.put(row, 0.0); self.loop.run(self.receiver)
        for step in range(1, 7):
            self.clock.at = 1000.0 + step * 50; row['counters']['reply']['packets'] += 5; self.loop.run(self.receiver)
        self.assertEqual(self.loop.counters['closed:maximum lifetime'], 1)

    def test_slots_bound_the_shadow_table_and_a_withdrawn_inventory_clears_it(self):
        # Fat enough to clear the scarcity bar, so only the hard bound stops the fifth and sixth.
        for n in range(6): self.receiver.put(stream_row('r%d' % n, ct=n, octets=(1 << 20, 32 << 20)), 0.0)
        status = self.loop.run(self.receiver); self.assertEqual(len(self.loop.leases), 4)
        self.assertEqual(status['last_pass']['reasons'], {'admitted': 4, 'policy: not now': 2})
        self.receiver.ready = False; self.loop.run(self.receiver)
        self.assertEqual(self.loop.leases, {}); self.assertEqual(self.loop.counters['closed:inventory withdrawn'], 4)

    def test_the_cursor_rotates_so_a_large_inventory_is_covered_over_passes(self):
        for n in range(10): self.receiver.put(stream_row('r%02d' % n, ct=n), 0.0)
        loop = AdmissionLoop('observe', clock=self.clock, slots=100)
        loop.run(self.receiver, bound=4); loop.run(self.receiver, bound=4); loop.run(self.receiver, bound=4)
        self.assertEqual(len(loop.leases), 10)

    def test_off_mode_does_nothing(self):
        self.receiver.put(stream_row('s'), 0.0)
        status = AdmissionLoop('off', clock=self.clock).run(self.receiver)
        self.assertEqual((status['leases'], status['counters'].get('admitted', 0)), (0, 0))


class FakeLifecycle:
    def __init__(self, fail_open=False):
        self.events = []; self.started = []; self.fenced = []; self.synchronized = False; self.fail_open = fail_open

    def start(self, producer, revision, digest):
        self.started.append((producer, revision, digest)); self.synchronized = True; self.sequence = 0

    def fence(self, reason): self.fenced.append(reason); self.synchronized = False

    def event(self, producer, sequence, operation, payload):
        if not self.synchronized: raise RuntimeError('session producer is not synchronized')
        if sequence != self.sequence + 1: raise RuntimeError('session stream lost continuity; resynchronize')
        if operation == 'open' and self.fail_open: raise RuntimeError('policy admission blocked')
        self.sequence = sequence; self.events.append((operation, payload))

    def status(self): return dict(synchronized=self.synchronized, sequence=getattr(self, 'sequence', 0))


def build_request(row, receiver, owner):
    return dict(session_id=row['conntrack_id'], revision=owner['revision'], policy_digest=owner['digest'],
                original=row['original'], reply=row['reply'], decision='d' * 64)


class AdmitModeTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(); self.receiver = Receiver(); self.owner = dict(revision=530, digest='c' * 64)

    def test_admit_mode_needs_its_collaborators(self):
        with self.assertRaises(ValueError): AdmissionLoop('admit')
        with self.assertRaises(ValueError): AdmissionLoop('nope')

    def test_the_lifecycle_is_started_on_the_barrier_and_driven_by_ordered_events(self):
        lifecycle = FakeLifecycle()
        loop = AdmissionLoop('admit', clock=self.clock, lifecycle=lifecycle, request=build_request, slots=8)
        row = stream_row('s', ct=4242); self.receiver.put(row, 900.0)
        status = loop.run(self.receiver, self.owner)
        self.assertEqual(lifecycle.started, [(self.receiver.producer, 530, 'c' * 64)])
        self.assertEqual([e[0] for e in lifecycle.events], ['heartbeat', 'open'])
        self.assertEqual(lifecycle.events[1][1]['session_id'], 4242); self.assertTrue(status['hardware_admission'])
        self.clock.at = 1020.0; row['counters']['original']['packets'] += 3; loop.run(self.receiver, self.owner)
        self.assertEqual(lifecycle.events[-1][0], 'refresh')
        self.receiver.drop('s'); loop.run(self.receiver, self.owner)
        self.assertEqual(lifecycle.events[-1], ('close', dict(session_id=4242)))
        self.assertEqual(loop.sequence, len(lifecycle.events))

    def test_a_failed_admission_counts_toward_back_off_and_is_not_retried_at_once(self):
        lifecycle = FakeLifecycle(fail_open=True)
        loop = AdmissionLoop('admit', clock=self.clock, lifecycle=lifecycle, request=build_request)
        for n in range(FAILURE_BACKOFF + 2): self.receiver.put(stream_row('r%d' % n, ct=n), 0.0)
        status = loop.run(self.receiver, self.owner)
        self.assertEqual(status['counters']['failed'], FAILURE_BACKOFF, 'after the back-off threshold the policy admits nothing more this pass')
        self.assertEqual(status['leases'], 0)
        self.clock.at += FAILURE_RETRY / 2; status = loop.run(self.receiver, self.owner)
        self.assertEqual(status['counters']['failed'], FAILURE_BACKOFF, 'recently failed rows are not retried before FAILURE_RETRY')

    def test_a_new_producer_restarts_the_lease_and_forgets_old_leases(self):
        lifecycle = FakeLifecycle()
        loop = AdmissionLoop('admit', clock=self.clock, lifecycle=lifecycle, request=build_request)
        self.receiver.put(stream_row('s'), 0.0); loop.run(self.receiver, self.owner); self.assertEqual(loop.leases.keys(), {'s'})
        self.receiver.producer = dict(self.receiver.producer, pid=5); self.receiver.sessions.clear(); self.receiver.since.clear()
        loop.run(self.receiver, self.owner)
        self.assertEqual(len(lifecycle.started), 2); self.assertEqual(loop.leases, {}); self.assertEqual(loop.sequence, 1)


if __name__ == '__main__':
    unittest.main()
