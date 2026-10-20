"""The tracked-routes convergence check: each link-tracked route's installed/reason on the DP.

2026-10-20: the MP route-link feed was stopped with the control daemon by a core patch
install and never started again; after 60 s the dataplane's link observation was stale,
the route tracker withdrew both default routes (`hardware-link-observation-stale`) and
nothing reported it for fifteen minutes. This check reads the tracker's own state on the
DP and says so, as the `tracked-routes` subsystem the core's convergence report and the
alert evaluator carry."""
import json
import unittest
from unittest.mock import AsyncMock

import daemon_backend as db

DP_BOOT = '9fed3cf5-0000-4000-8000-00000000dp01'
DEFAULT4 = dict(dst='0.0.0.0/0', via='184.187.55.65', dev='p1', track_link=True)
DEFAULT6 = dict(dst='::/0', via='fe80::1', dev='p1', track_link=True)
NETWORK = dict(revision=12, ports={'p1': dict(addresses=['184.187.55.66/28'])}, routes=[DEFAULT4, DEFAULT6, dict(dst='10.9.0.0/16', dev='p5')])


def tracker(routes, observed=1000.0, boot=DP_BOOT, revision=12):
    records = {}
    for i, (route, installed, reason) in enumerate(routes):
        records['key%d' % i] = dict(route=route, installed=installed, reason=reason, observed=observed)
    return dict(boot=boot, observed=observed, revision=revision, routes=records)


def observation(age=2.0, uptime=1001.0, boot=DP_BOOT, links=None):
    return dict(boot_id=boot, revision=12, links=links or {'p1': True}, observed=uptime - age)


def collection(state, obs, network, uptime=1001.0, boot=DP_BOOT):
    pieces = [json.dumps(state) if state is not None else '', json.dumps(obs) if obs is not None else '',
              json.dumps(network) if network is not None else '']
    return (db.TRACKED_ROUTES_SPLIT + '\n').join(pieces + ['%s 3998.12\n%s\n' % (uptime, boot)])


class Report(unittest.TestCase):
    def test_every_tracked_route_installed_is_converged(self):
        state = tracker([(DEFAULT4, True, 'eligible'), (DEFAULT6, True, 'eligible')])
        result = db.tracked_routes_report(state, observation(), NETWORK, 1001.0, DP_BOOT, feed_state='active')
        self.assertEqual((result['state'], result['summary']), ('converged', '2 tracked route(s) installed'))
        self.assertEqual(result['details'], ['0.0.0.0/0 via 184.187.55.65 dev p1: installed', '::/0 via fe80::1 dev p1: installed',
                                             'link observation 2 s old', 'MP feed ffn-route-link-feed.service active'])
        self.assertEqual([r['installed'] for r in result['routes']], [True, True])
        self.assertEqual(result['observation'], dict(present=True, age_seconds=2.0, boot_matches=True))
        self.assertEqual(result['feed'], dict(unit='ffn-route-link-feed.service', state='active'))
        self.assertTrue(result['tracker']['reported'])

    def test_a_route_withdrawn_for_a_stale_observation_is_failed(self):
        # The 2026-10-20 picture: the tracker is alive, the observation is 900 s old, both defaults withdrawn.
        state = tracker([(DEFAULT4, False, 'hardware-link-observation-stale'), (DEFAULT6, False, 'hardware-link-observation-stale')])
        result = db.tracked_routes_report(state, observation(age=900), NETWORK, 1001.0, DP_BOOT, feed_state='inactive')
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['summary'], '2 tracked route(s) withdrawn: hardware link observation stale on the dataplane')
        self.assertIn('0.0.0.0/0 via 184.187.55.65 dev p1: withdrawn (hardware-link-observation-stale)', result['details'])
        self.assertIn('link observation 900 s old (stale: tracked routes withdrawn)', result['details'])
        self.assertIn('MP feed ffn-route-link-feed.service inactive', result['details'])
        self.assertEqual(result['routes'][0]['reason'], 'hardware-link-observation-stale')
        # No observation file at all: the same verdict, said plainly.
        state = tracker([(DEFAULT4, False, 'hardware-link-observation-unavailable')])
        result = db.tracked_routes_report(state, None, NETWORK, 1001.0, DP_BOOT)
        self.assertEqual(result['state'], 'failed')
        self.assertIn('no link observation on the dataplane', result['details'])
        self.assertEqual(result['observation'], dict(present=False, age_seconds=None, boot_matches=None))
        # An observation from another DP boot is stale however young it is.
        result = db.tracked_routes_report(state, observation(boot='other-boot'), NETWORK, 1001.0, DP_BOOT)
        self.assertIn('link observation 2 s old from another DP boot (stale: tracked routes withdrawn)', result['details'])

    def test_a_route_waiting_for_its_link_is_pending_not_failed(self):
        for reason in sorted(db.WAITING_REASONS):
            with self.subTest(reason=reason):
                state = tracker([(DEFAULT4, False, reason), (DEFAULT6, True, 'eligible')])
                result = db.tracked_routes_report(state, observation(), NETWORK, 1001.0, DP_BOOT)
                self.assertEqual((result['state'], result['summary']), ('pending', '1 of 2 tracked route(s) waiting for a link'))

    def test_a_route_that_cannot_be_installed_as_committed_is_failed(self):
        for reason in ('gateway-outside-interface-prefix', 'interface-missing', 'something-new'):
            with self.subTest(reason=reason):
                state = tracker([(DEFAULT4, False, reason)])
                result = db.tracked_routes_report(state, observation(), NETWORK, 1001.0, DP_BOOT)
                self.assertEqual((result['state'], result['summary']), ('failed', '1 tracked route(s) cannot be installed as committed'))
        # The feed takes precedence: fix the observation before judging the rest.
        state = tracker([(DEFAULT4, False, 'interface-missing'), (DEFAULT6, False, 'hardware-link-observation-stale')])
        self.assertIn('observation stale', db.tracked_routes_report(state, observation(age=70), NETWORK, 1001.0, DP_BOOT)['summary'])

    def test_a_stalled_tracker_is_failed(self):
        state = tracker([(DEFAULT4, True, 'eligible')], observed=900.0)   # last tick 101 s ago; its stop hook withdrew the route
        result = db.tracked_routes_report(state, observation(), NETWORK, 1001.0, DP_BOOT)
        self.assertEqual((result['state'], result['summary']), ('failed', 'Route tracker stalled: last tick 101 s ago'))
        self.assertEqual(result['tracker']['age_seconds'], 101.0)
        broken = dict(tracker([(DEFAULT4, True, 'eligible')]), observed='soon')
        self.assertEqual(db.tracked_routes_report(broken, observation(), NETWORK, 1001.0, DP_BOOT)['summary'], 'Route tracker stalled: last tick ? s ago')

    def test_before_the_tracker_reports_the_committed_routes_are_pending(self):
        result = db.tracked_routes_report(None, None, NETWORK, 50.0, DP_BOOT)
        self.assertEqual((result['state'], result['summary']), ('pending', 'Route tracker has not reported on this dataplane boot'))
        self.assertEqual(result['details'][:2], ['0.0.0.0/0 via 184.187.55.65 dev p1: not yet judged', '::/0 via fe80::1 dev p1: not yet judged'])
        self.assertEqual([r['reason'] for r in result['routes']], ['tracker-not-reported'] * 2)
        self.assertFalse(result['tracker']['reported'])
        # A state file from the previous DP boot is no report for this one.
        stale = tracker([(DEFAULT4, True, 'eligible')], boot='previous-boot')
        self.assertEqual(db.tracked_routes_report(stale, observation(), NETWORK, 50.0, DP_BOOT)['state'], 'pending')
        # Nothing tracked, nothing reported: converged, there is nothing to wait for.
        untracked = dict(NETWORK, routes=[dict(dst='10.9.0.0/16', dev='p5')])
        self.assertEqual(db.tracked_routes_report(None, None, untracked, 50.0, DP_BOOT)['summary'], 'No link-tracked routes on the dataplane')
        self.assertEqual(db.tracked_routes_report(None, None, None, 50.0, DP_BOOT)['state'], 'converged')
        self.assertEqual(db.tracked_routes_report(tracker([]), observation(), untracked, 1001.0, DP_BOOT)['summary'], 'No link-tracked routes on the dataplane')

    def test_a_tracker_behind_the_network_revision_is_pending(self):
        state = tracker([(DEFAULT4, True, 'eligible')], revision=11)
        result = db.tracked_routes_report(state, observation(), NETWORK, 1001.0, DP_BOOT)
        self.assertEqual((result['state'], result['summary']), ('pending', 'Route tracker has not applied network revision 12'))

    def test_the_collection_is_one_round_trip(self):
        state = tracker([(DEFAULT4, True, 'eligible')])
        parsed = db.parse_tracked_routes(collection(state, observation(), NETWORK))
        self.assertEqual(parsed, (state, observation(), NETWORK, 1001.0, DP_BOOT))
        self.assertEqual(db.parse_tracked_routes(collection(None, None, None))[:3], (None, None, None))
        self.assertEqual(db.parse_tracked_routes(collection('not json', [1, 2], NETWORK))[:2], (None, None))
        with self.assertRaises(ValueError):
            db.parse_tracked_routes('garbage')
        with self.assertRaises(ValueError):
            db.parse_tracked_routes((db.TRACKED_ROUTES_SPLIT + '\n') * 3)
        self.assertIn('cat /run/ffn-static-routes.json', db.TRACKED_ROUTES_COLLECT)
        self.assertIn('cat /run/ffn-route-links.json', db.TRACKED_ROUTES_COLLECT)
        self.assertIn('/proc/sys/kernel/random/boot_id', db.TRACKED_ROUTES_COLLECT)

    def test_the_feed_unit_state_is_systemd_s_word(self):
        calls = []
        def run(argv, **kw):
            calls.append(argv); return type('R', (), dict(returncode=3, stdout='inactive\n', stderr=''))()
        self.assertEqual(db.feed_unit_state(run=run), 'inactive')
        self.assertEqual(calls, [['systemctl', 'is-active', 'ffn-route-link-feed.service']])
        def missing(*a, **k):
            raise OSError('no systemctl')
        self.assertIsNone(db.feed_unit_state(run=missing))


class Resource(unittest.IsolatedAsyncioTestCase):
    async def test_status_reads_the_dataplane_once_and_never_touches_the_controller(self):
        backend = AsyncMock()
        state = tracker([(DEFAULT4, False, 'hardware-link-observation-stale')])
        text = collection(state, observation(age=120), NETWORK)
        collected = []
        def collect():
            collected.append(1); return text
        result = await db.execute('tracked-routes', 'status', {}, backend, collect=collect, unit_state=lambda: 'inactive')
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['feed']['state'], 'inactive')
        self.assertEqual(collected, [1])
        backend.run.assert_not_awaited()
        with self.assertRaisesRegex(ValueError, 'no payload'):
            await db.execute('tracked-routes', 'status', {'x': 1}, backend, collect=collect)
        def broken():
            raise RuntimeError('Dataplane collection failed: ssh: connect timed out')
        with self.assertRaises(RuntimeError):
            await db.execute('tracked-routes', 'status', {}, backend, collect=broken, unit_state=lambda: None)
        self.assertEqual(db.error_response(RuntimeError('ssh said something')), 'MP hardware adapter rejected or could not complete the operation')


if __name__ == '__main__':
    unittest.main()
