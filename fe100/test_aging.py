#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""Idle expiry must drain the table without ever tearing down live traffic.

Both halves matter and they pull against each other. A sweep that never expires
anything lets the FE100 flow table fill until installs fail; a sweep that
expires too eagerly drops a customer's connection. So the tests are paired:
for every "this does get expired" there is a "this does not", and the
not-expired cases are the ones that would be caught in production rather than
in review.

Runs against the real SessionManager with a fake backend -- the aging policy is
worth nothing if it does not compose with the actual install/remove lifecycle,
including its journalling and its recovery interlock.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ffn_fe100_aging import SessionAging, protocol_of, fetch_probe  # noqa: E402
from ffn_fe100_sessions import (SessionManager, key4, forwarding_entry4)  # noqa: E402

TCP, UDP = 6, 17


class Backend:
    """Minimal in-memory stand-in for the qualified hardware adapter."""

    def __init__(self):
        self.flows = {}

    def readiness(self):
        return []

    def fetch(self, key):
        return self.flows.get(bytes(key))

    def insert(self, entry):
        self.flows[bytes(entry[:16])] = bytes(entry)

    def delete(self, key):
        self.flows.pop(bytes(key), None)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Probe:
    """Per-entry activity counters the test drives by hand."""

    def __init__(self):
        self.counters = {}
        self.unreadable = set()

    def __call__(self, entry):
        key = bytes(entry[:16])
        if key in self.unreadable:
            return None
        return self.counters.get(key, 0)

    def bump(self, entry, by=1):
        key = bytes(entry[:16])
        self.counters[key] = self.counters.get(key, 0) + by


class Base(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()
        self.manager = SessionManager(self.backend)
        self.clock = Clock()
        self.probe = Probe()
        self.aging = SessionAging(self.manager, self.probe, clock=self.clock)
        self.next_flow = 100

    def session(self, ident, protocol=UDP, sport=1024, dport=53):
        """Install one bidirectional session and return its two entries."""
        forward = key4('192.0.2.10', '198.51.100.7', sport, dport, protocol, 4094)
        reverse = key4('198.51.100.7', '192.0.2.10', dport, sport, protocol, 4094)
        entries = (forwarding_entry4(forward, self.next_flow, 31),
                   forwarding_entry4(reverse, self.next_flow + 1, 31))
        self.next_flow += 2
        self.manager.install(ident, entries, revision=1)
        return entries


class Expiry(Base):
    def test_a_silent_session_is_expired_after_its_timeout(self):
        self.session(1, UDP)
        self.aging.sweep()                      # first sight starts the clock
        self.clock.advance(31)                  # UDP default is 30 s
        out = self.aging.sweep()
        self.assertEqual(out['expired'], [1])
        self.assertNotIn(1, self.manager.sessions)
        self.assertEqual(self.backend.flows, {}, 'hardware entries were left behind')

    def test_a_session_is_never_expired_on_first_sight(self):
        """One observation is not evidence of silence, however old the clock."""
        self.session(1, UDP)
        self.clock.advance(100000)
        self.assertEqual(self.aging.sweep()['expired'], [])
        self.assertIn(1, self.manager.sessions)

    def test_traffic_on_either_direction_keeps_the_session(self):
        for direction in (0, 1):
            with self.subTest(direction=direction):
                self.setUp()
                entries = self.session(1, UDP)
                self.aging.sweep()
                for _ in range(5):
                    self.clock.advance(20)
                    self.probe.bump(entries[direction])
                    self.aging.sweep()
                self.clock.advance(20)
                self.assertEqual(self.aging.sweep()['expired'], [],
                                 'a one-way flow is still a live flow')

    def test_silence_is_measured_from_the_last_change_not_from_install(self):
        entries = self.session(1, UDP)
        self.aging.sweep()
        self.clock.advance(25); self.probe.bump(entries[0]); self.aging.sweep()
        self.clock.advance(25)
        self.assertEqual(self.aging.sweep()['expired'], [], 'timer did not reset')
        self.clock.advance(6)
        self.assertEqual(self.aging.sweep()['expired'], [1])

    def test_tcp_and_udp_use_different_timeouts(self):
        self.session(1, UDP)
        self.session(2, TCP, sport=2048, dport=443)
        self.aging.sweep()
        self.clock.advance(31)
        self.assertEqual(self.aging.sweep()['expired'], [1], 'UDP should go first')
        self.clock.advance(3600)
        self.assertEqual(self.aging.sweep()['expired'], [2])

    def test_a_protocol_with_no_timeout_is_never_expired(self):
        self.session(1, UDP)
        self.aging.timeouts = {}
        self.aging.sweep()
        self.clock.advance(100000)
        self.assertEqual(self.aging.sweep()['expired'], [])


class FailsSafe(Base):
    def test_an_unreadable_probe_never_expires_a_session(self):
        """A read failure is not silence. Expiring on it drops live traffic."""
        entries = self.session(1, UDP)
        self.aging.sweep()
        self.probe.unreadable.add(bytes(entries[0][:16]))
        for _ in range(10):
            self.clock.advance(60)
            out = self.aging.sweep()
            self.assertEqual(out['expired'], [])
            self.assertEqual(out['unreadable'], 1)
        self.assertIn(1, self.manager.sessions)

    def test_recovering_the_readable_state_restarts_the_clock(self):
        entries = self.session(1, UDP)
        self.aging.sweep()
        self.probe.unreadable.add(bytes(entries[0][:16]))
        self.clock.advance(1000)
        self.aging.sweep()
        self.probe.unreadable.clear()
        self.clock.advance(20)
        self.assertEqual(self.aging.sweep()['expired'], [],
                         'silence accrued while blind was counted against it')

    def test_nothing_is_expired_while_recovery_is_required(self):
        self.session(1, UDP)
        self.aging.sweep()
        self.clock.advance(100000)
        self.manager.recovery_required = True
        out = self.aging.sweep()
        self.assertEqual(out['expired'], [])
        self.assertEqual(out['skipped'], 'recovery required')
        self.assertIn(1, self.manager.sessions)

    def test_only_installed_sessions_are_aged(self):
        self.session(1, UDP)
        self.manager.sessions[1]['state'] = 'installing'
        self.aging.sweep()
        self.clock.advance(100000)
        self.assertEqual(self.aging.sweep()['expired'], [])

    def test_a_failed_removal_stops_the_sweep_rather_than_continuing(self):
        self.session(1, UDP)
        self.session(2, UDP, sport=1025)
        self.aging.sweep()
        self.clock.advance(31)
        # The adapter reports someone else now owns the flow.
        original = self.backend.fetch
        self.backend.fetch = lambda key: b'\x40' + bytes(63)
        out = self.aging.sweep()
        self.backend.fetch = original
        self.assertEqual(out['expired'], [])
        self.assertEqual(len(out['failed']), 1)
        self.assertTrue(self.manager.recovery_required)

    def test_fetch_is_refused_as_an_activity_probe(self):
        """The obvious wrong probe must fail loudly, not silently expire all."""
        with self.assertRaises(NotImplementedError) as raised:
            fetch_probe(self.backend)
        self.assertIn('byte-stable', str(raised.exception))


class Bounded(Base):
    def test_removals_are_capped_per_sweep_and_resume_next_time(self):
        self.aging.max_removals = 2
        for ident in range(1, 6):
            self.session(ident, UDP, sport=1024 + ident)
        self.aging.sweep()
        self.clock.advance(31)
        first = self.aging.sweep()
        self.assertEqual(len(first['expired']), 2)
        self.assertEqual(first['deferred'], 3)
        second = self.aging.sweep()
        self.assertEqual(len(second['expired']), 2)
        third = self.aging.sweep()
        self.assertEqual(len(third['expired']), 1)
        self.assertEqual(self.manager.sessions, {})

    def test_tracking_state_does_not_grow_after_sessions_leave(self):
        for ident in range(1, 4):
            self.session(ident, UDP, sport=1024 + ident)
        self.aging.sweep()
        self.assertEqual(len(self.aging._seen), 3)
        for ident in (1, 2, 3):
            self.manager.remove(ident)
        self.aging.sweep()
        self.assertEqual(self.aging._seen, {}, 'per-session state leaked')

    def test_max_removals_must_be_positive(self):
        with self.assertRaises(ValueError):
            SessionAging(self.manager, self.probe, max_removals=0)


class Tokens(Base):
    def test_a_wrapping_counter_reads_as_activity_not_silence(self):
        """Tokens are compared, never subtracted -- a wrap must not look idle."""
        entries = self.session(1, UDP)
        self.probe.counters[bytes(entries[0][:16])] = 0xFFFFFFFF
        self.aging.sweep()
        self.clock.advance(20)
        self.probe.counters[bytes(entries[0][:16])] = 0      # wrapped
        self.aging.sweep()
        self.clock.advance(20)
        self.assertEqual(self.aging.sweep()['expired'], [],
                         'a wrapped counter was mistaken for silence')

    def test_protocol_is_read_from_the_key(self):
        entries = self.session(1, TCP, sport=4096, dport=443)
        self.assertEqual(protocol_of(entries[0]), TCP)
        self.assertEqual(protocol_of(entries[1]), TCP)


if __name__ == '__main__':
    unittest.main(verbosity=2)
