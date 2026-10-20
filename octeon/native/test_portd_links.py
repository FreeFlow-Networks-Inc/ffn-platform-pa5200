#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""`ffn-portd links` records the route tracker's link observation beside the carrier lease.

No hardware, namespace, TAP or root: the built ffn-portd runs with its files redirected
into a temporary directory (FFN_PORTD_RUN, FFN_PORTD_NETWORK, FFN_PORTD_ROUTE_LINKS).
test-portd-linux.py proves the same delivery against real owners.

Why this exists: until 2026-10-20 the only writer of /run/ffn-route-links.json was the
MP's ffn-route-link-feed, through the control daemon; a core patch install stopped that
feed with the daemon and nothing started it again, so the observation went stale and
the dataplane withdrew both default routes for fifteen minutes. The delivery the port
owners receive every three seconds now writes the same record."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parent
BINARY = HERE / 'ffn-portd'
BOOT = Path('/proc/sys/kernel/random/boot_id')


def row(port, **fields):
    base = dict(port=port, media='sfp', available=True, enabled=True, link=True)
    base.update(fields)
    return base


@unittest.skipUnless(BINARY.exists() and BOOT.exists(), 'needs a built ffn-portd on Linux (make ffn-portd)')
class LinksTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ffn-portd-links-')
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.route_links = self.dir / 'route-links.json'
        self.env = dict(os.environ, FFN_PORTD_RUN=str(self.dir), FFN_PORTD_NETWORK=str(self.dir / 'network.json'),
                        FFN_PORTD_ROUTE_LINKS=str(self.route_links), LD_LIBRARY_PATH=str(HERE))
        self.sequence = 100

    def deliver(self, ports, sequence=None, **extra):
        if sequence is None:
            self.sequence += 1
            sequence = self.sequence
        payload = dict(mp_boot_id='mp-boot-test', sequence=sequence, age_seconds=0.25, lease_seconds=30, ports=ports)
        payload.update(extra)
        return subprocess.run([str(BINARY), 'links'], input=json.dumps(payload), env=self.env, capture_output=True, text=True, timeout=20)

    def test_the_observation_is_written_in_the_tracker_form(self):
        (self.dir / 'network.json').write_text(json.dumps(dict(revision=7, ports={}, routes=[dict(dst='0.0.0.0/0', dev='p1', track_link=True)])))
        result = self.deliver([row(1), row(5, link=False), row(9, available=False), dict(port=12, media='sfp', link=True),
                               row(2, media='copper', phy_enabled=True, mac_enabled=True, mac_link=True, datapath_link=True, speed_mbps=1000, mac_speed_mbps=1000)])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        reply = json.loads(result.stdout)
        self.assertEqual((reply['accepted'], reply['ports'], reply['route_links']), (True, 5, True))
        observation = json.loads(self.route_links.read_text())
        self.assertEqual(set(observation), {'boot_id', 'revision', 'links', 'observed'})
        self.assertEqual(observation['boot_id'], BOOT.read_text().strip())
        self.assertEqual(observation['revision'], 7)
        # available AND enabled AND link, each explicitly true: the MP feed's rule (daemon_backend.py).
        self.assertEqual(observation['links'], {'p1': True, 'p5': False, 'p9': False, 'p12': False, 'p2': True})
        self.assertTrue(0 <= time.monotonic() - observation['observed'] < 5, observation['observed'])
        self.assertEqual(oct(self.route_links.stat().st_mode & 0o777), oct(0o600))
        # The carrier lease is recorded as before, keyed by port number.
        lease = json.loads((self.dir / 'ffn-port-links.json').read_text())
        self.assertEqual(sorted(lease['ports']), ['1', '12', '2', '5', '9'])
        self.assertEqual(lease['sequence'], self.sequence)

    def test_a_link_change_is_the_next_delivery(self):
        self.assertEqual(self.deliver([row(1)]).returncode, 0)
        self.assertEqual(json.loads(self.route_links.read_text())['links'], {'p1': True})
        self.assertEqual(self.deliver([row(1, link=False)]).returncode, 0)
        self.assertEqual(json.loads(self.route_links.read_text())['links'], {'p1': False})

    def test_without_a_network_journal_the_revision_is_null(self):
        self.assertEqual(self.deliver([row(1)]).returncode, 0)
        observation = json.loads(self.route_links.read_text())
        self.assertIsNone(observation['revision'])
        self.assertEqual(observation['links'], {'p1': True})

    def test_a_refused_delivery_leaves_the_observation_alone(self):
        self.assertEqual(self.deliver([row(1)]).returncode, 0)
        before = self.route_links.read_text()
        replay = self.deliver([row(1, link=False)], sequence=self.sequence - 1)
        self.assertNotEqual(replay.returncode, 0)
        self.assertIn('stale', replay.stdout)
        invalid = self.deliver([row(1, link=False)], age_seconds=-1)
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn('invalid', invalid.stdout)
        self.assertEqual(self.route_links.read_text(), before)
        self.assertEqual(json.loads(before)['links'], {'p1': True})


if __name__ == '__main__':
    unittest.main()
