#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""Tests for ffn_queue_provision: the marker parser and the convergence plan.

Runs with no hardware. The interesting cases are the ones that must NOT be
proposed for allocation -- a partially allocated port owns a resource the
allocate path will not unwind, so treating it as "missing" would leak a
connector every time somebody re-ran provisioning.

The real captures in this directory are used as the happy path, so a change to
the marker format that the parser cannot read fails here rather than on a box.
"""
import json
import os
import unittest

import ffn_queue_provision as qp

HERE = os.path.dirname(os.path.abspath(__file__))


def markers(port, stages=("port", "connector", "attach", "voq"), rv=0, queues=8):
    """Synthesise a capture for one port, truncated after `stages`."""
    text = {
        "port": "FFN_PORT dst=%d core=0 tm=%d priorities=2 modid=0" % (port, port),
        "connector": "FFN_CONNECTOR dst=%d gport=0xc4000010 rv=%d" % (port, rv),
        "attach": "FFN_ATTACH dst=%d queues=%d rv=%d" % (port, queues, rv),
        "voq": "FFN_VOQ dst=%d sysport=0x6c000018 gport=0x243c0004 rv=%d" % (port, rv),
    }
    return [text[s] for s in stages]


class Parser(unittest.TestCase):
    def test_stages_and_fields(self):
        ports, done, order = qp.parse_markers(markers(24) + ["FFN_DONE"])
        self.assertTrue(done)
        self.assertEqual(order, [24])
        rec = ports[24]
        self.assertEqual(set(rec), {"port", "connector", "attach", "voq"})
        self.assertEqual(rec["attach"]["queues"], "8")
        self.assertEqual(rec["voq"]["gport"], "0x243c0004")
        self.assertEqual(rec["port"]["core"], "0")

    def test_done_absent_is_reported(self):
        _, done, _ = qp.parse_markers(markers(24))
        self.assertFalse(done)

    def test_unrelated_lines_ignored(self):
        ports, _, _ = qp.parse_markers(
            ["FFN_DP_ALLOCATION_PREFLIGHT queues=0 rv=0", "noise", ""] + markers(7))
        self.assertEqual(list(ports), [7])


class Classify(unittest.TestCase):
    def one(self, lines, want=8):
        ports, _, _ = qp.parse_markers(lines)
        rec = ports.get(next(iter(ports), None)) if ports else None
        return qp.classify(rec, want)

    def test_complete(self):
        state, _ = self.one(markers(24))
        self.assertEqual(state, qp.COMPLETE)

    def test_absent_is_missing(self):
        state, _ = qp.classify(None, 8)
        self.assertEqual(state, qp.MISSING)

    def test_partial_is_not_missing(self):
        """The whole point: a half-allocated port must never look allocatable."""
        for stages in (("port",), ("port", "connector"), ("port", "connector", "attach")):
            state, detail = self.one(markers(24, stages=stages))
            self.assertEqual(state, qp.PARTIAL, stages)
            self.assertNotEqual(state, qp.MISSING)
            self.assertIn("does not unwind", detail)

    def test_nonzero_rv_is_failed(self):
        state, detail = self.one(markers(24, rv=-8))
        self.assertEqual(state, qp.FAILED)
        self.assertIn("rv=-8", detail)

    def test_queue_count_mismatch(self):
        state, detail = self.one(markers(24, queues=4), want=8)
        self.assertEqual(state, qp.MISMATCH)
        self.assertIn("attached 4", detail)

    def test_no_desired_count_accepts_any(self):
        state, _ = self.one(markers(24, queues=4), want=None)
        self.assertEqual(state, qp.COMPLETE)


class Plan(unittest.TestCase):
    def test_truncated_capture_flags_the_last_port(self):
        ports, done, order = qp.parse_markers(markers(24) + markers(28, stages=("port",)))
        out = qp.plan(ports, {"24": {"queues": 8}, "28": {"queues": 8}}, done, order)
        self.assertEqual(out[24][0], qp.COMPLETE)
        self.assertEqual(out[28][0], qp.PARTIAL)
        self.assertIn("indeterminate", out[28][1])

    def test_later_capture_wins(self):
        with_partial = {"markers": markers(14, stages=("port", "connector"))}
        with_full = {"markers": markers(14) + ["FFN_DONE"]}
        obs, _, _ = qp.parse_markers(with_partial["markers"])
        obs2, _, _ = qp.parse_markers(with_full["markers"])
        obs.setdefault(14, {}).update(obs2[14])
        self.assertEqual(qp.classify(obs[14], 8)[0], qp.COMPLETE)


class AgainstRealCaptures(unittest.TestCase):
    """The shipped profile against the captures actually in this directory."""

    def setUp(self):
        self.profile = qp.load_profile(os.path.join(HERE, "queue-profile.json"))
        self.captures = qp.default_captures()

    def test_captures_are_found_and_parse(self):
        self.assertTrue(self.captures, "no DP-QUEUES / FE100-SESSION captures found")
        observed, done, order, sources = qp.load_observed(self.captures)
        self.assertTrue(done, "a shipped capture has no FFN_DONE")
        for port in (3, 7, 8, 14, 16, 24, 28, 34, 35):
            self.assertIn(port, observed, "port %d missing from the captures" % port)

    def test_plan_is_complete_except_the_port_no_capture_covers(self):
        observed, done, order, _ = qp.load_observed(self.captures)
        result = qp.plan(observed, self.profile["ports"], done, order)
        states = {p: s for p, (s, _, _) in result.items()}
        # 15 was allocated during the copper work on 2026-09-16, after both
        # captures here were taken. Reporting it as MISSING is the correct
        # answer to "is our recorded evidence current?" -- not a bug.
        self.assertEqual(states.pop(15), qp.MISSING)
        self.assertEqual(set(states.values()), {qp.COMPLETE},
                         "unexpected states: %s" % states)

    def test_every_profile_port_has_a_role(self):
        for port, spec in self.profile["ports"].items():
            self.assertTrue(spec.get("role"), "port %s has no role" % port)
            self.assertEqual(spec.get("queues"), 8, "port %s" % port)

    def test_profile_pins_no_allocation_handles(self):
        """Handles change across a restart; a profile must not encode one."""
        blob = json.dumps(self.profile["ports"])
        for stale in ("0xc4", "0x243c", "0x6c00"):
            self.assertNotIn(stale, blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
