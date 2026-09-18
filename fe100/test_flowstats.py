#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""Statistics decoding, and the probe contract ffn_fe100_aging depends on.

The decode tests pin the bit layout against the vendor's own field widths --
flow_idx 32, packets 30, octets 42 -- because a silently wrong shift produces
plausible small numbers rather than an error, and the symptom would be sessions
expiring while they carry traffic.

The demux tests pin BOTH halves of the message identity. Keying on the type
byte alone is the mistake this module was written with: MSG_TYPE_FLOWSTATS (19)
is defined in the vendor's enum and used nowhere, while sessionCount actually
arrives as CONTROL/STATS_COUNTER. Type 19 must be ignored and CONTROL with some
other code -- session ageout, say -- must be ignored too, or the decoder would
read a session-teardown payload as counters.

The probe tests are mostly about one distinction: an unknown flow returns None,
not 0. Getting that wrong means a session whose first statistics message has not
arrived yet starts its idle clock immediately.
"""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ffn_fe100_flowstats as FS  # noqa: E402
from ffn_fe100_aging import SessionAging  # noqa: E402
from ffn_fe100_sessions import key4, forwarding_entry4  # noqa: E402

CTRL_CODE_SESS_AGEOUT = 4        # the neighbouring code in the same family
MSG_TYPE_FLOWSTATS = 19          # the decoy; defined by the vendor, never used


def record(flow_id, packets, octets):
    """Build a sessionCount record the way the FE100 lays it out."""
    return struct.pack('!QQ', (flow_id << 32) | packets, octets)


def header(msg_type=FS.MSG_TYPE_CONTROL, code=FS.CTRL_CODE_STATS_COUNTER):
    """A 32-byte fe100ToOcteonHdr with type at byte 3 and code at byte 23."""
    head = bytearray(FS.HEADER_SIZE)
    head[FS.TYPE_OFFSET] = msg_type
    head[FS.CODE_OFFSET] = code
    return bytes(head)


class Decode(unittest.TestCase):
    def test_field_positions(self):
        self.assertEqual(FS.decode_record(record(0x11223344, 5, 9)),
                         (0x11223344, 5, 9))

    def test_each_field_at_its_full_width(self):
        flow_id, packets, octets = 0xFFFFFFFF, (1 << 30) - 1, (1 << 42) - 1
        self.assertEqual(FS.decode_record(record(flow_id, packets, octets)),
                         (flow_id, packets, octets))

    def test_fields_do_not_bleed_into_each_other(self):
        """A maxed neighbour must not show up in the field being read."""
        self.assertEqual(FS.decode_record(record(0xFFFFFFFF, 0, 0)),
                         (0xFFFFFFFF, 0, 0))
        self.assertEqual(FS.decode_record(record(0, (1 << 30) - 1, 0)),
                         (0, (1 << 30) - 1, 0))
        self.assertEqual(FS.decode_record(record(0, 0, (1 << 42) - 1)),
                         (0, 0, (1 << 42) - 1))

    def test_the_pad_bits_are_ignored(self):
        """pad0 sits between flow_idx and packets; pad1 above octets."""
        word0 = (0xABCD << 32) | (0b11 << 30) | 7      # pad0 set
        word1 = (0x3FFFFF << 42) | 11                  # pad1 set
        self.assertEqual(FS.decode_record(struct.pack('!QQ', word0, word1)),
                         (0xABCD, 7, 11))

    def test_a_record_must_be_sixteen_bytes(self):
        for bad in (b'', bytes(15), bytes(17)):
            with self.assertRaises(ValueError):
                FS.decode_record(bad)

    def test_multiple_records_in_one_payload(self):
        payload = record(1, 10, 100) + record(2, 20, 200) + record(3, 30, 300)
        self.assertEqual(FS.decode_records(payload),
                         [(1, 10, 100), (2, 20, 200), (3, 30, 300)])

    def test_a_truncated_payload_is_rejected_by_the_payload_check(self):
        """Asserting the message, not just that something raised.

        Any non-multiple of 16 also trips the per-record length check, so a
        bare assertRaises passes even with the payload guard deleted -- which
        a mutation run duly demonstrated. The guard earns its place by naming
        the real fault (a truncated message) instead of blaming the last
        record, so the test pins which check fired.
        """
        with self.assertRaises(ValueError) as raised:
            FS.decode_records(record(1, 2, 3) + bytes(8))
        self.assertIn('whole number of records', str(raised.exception))

    def test_empty_payload_is_zero_records(self):
        self.assertEqual(FS.decode_records(b''), [])


class Demux(unittest.TestCase):
    """Which messages carry sessionCount, and which only look like they do."""

    def test_control_plus_stats_counter_is_the_statistics_message(self):
        self.assertTrue(FS.is_stats_message(header()))

    def test_the_flowstats_message_type_is_not_it(self):
        """MSG_TYPE_FLOWSTATS is the decoy. Keying on it reads nothing.

        The vendor defines type 19 and never emits or parses it; their own
        decoder demuxes sessionCount on (CONTROL, STATS_COUNTER).
        """
        self.assertFalse(FS.is_stats_message(
            header(msg_type=MSG_TYPE_FLOWSTATS)))
        self.assertFalse(FS.is_stats_message(
            header(msg_type=MSG_TYPE_FLOWSTATS, code=0)))

    def test_control_with_another_code_is_not_it(self):
        """CONTROL also carries setup, update, remove and ageout.

        Decoding one of those as records would read a teardown payload as
        packet counts.
        """
        for code in (1, 2, 3, CTRL_CODE_SESS_AGEOUT, 6):
            with self.subTest(code=code):
                self.assertFalse(FS.is_stats_message(header(code=code)))

    def test_the_right_code_under_the_wrong_type_is_not_it(self):
        """Code 5 means something else entirely outside CONTROL."""
        for msg_type in (0, 1, 2, 15, 16, 17, 18, MSG_TYPE_FLOWSTATS):
            with self.subTest(msg_type=msg_type):
                self.assertFalse(FS.is_stats_message(header(msg_type=msg_type)))

    def test_the_header_is_a_fixed_thirty_two_bytes(self):
        """21 fields, 256 bits, no optional sections -- so records start at 32."""
        self.assertEqual(FS.HEADER_SIZE, 32)
        self.assertEqual(FS.header_length(header()), 32)
        self.assertEqual(FS.header_length(header() + record(1, 2, 3)), 32)

    def test_a_message_shorter_than_its_header_is_rejected(self):
        for bad in (b'', b'\x00\x00', bytes(FS.HEADER_SIZE - 1)):
            with self.subTest(length=len(bad)):
                with self.assertRaises(ValueError):
                    FS.is_stats_message(bad)
                with self.assertRaises(ValueError):
                    FS.header_length(bad)


class Probe(unittest.TestCase):
    def setUp(self):
        self.probe = FS.FlowStatsProbe()
        key = key4('192.0.2.10', '198.51.100.7', 1024, 53, 17, 4094)
        self.entry = forwarding_entry4(key, 0x1234, 31)

    def test_flow_id_is_read_from_the_entry(self):
        """Byte 36 -- flowEntry.flowid in the vendor layout."""
        self.assertEqual(FS.flow_id_of(self.entry), 0x1234)

    def test_an_unknown_flow_is_None_not_zero(self):
        """None means 'cannot read' and keeps the session; 0 would start its
        idle clock before any statistics had arrived."""
        self.assertIsNone(self.probe(self.entry))

    def test_a_flow_reported_with_zero_packets_is_zero_not_None(self):
        self.probe.consume_payload(record(0x1234, 0, 0))
        self.assertEqual(self.probe(self.entry), 0)

    def test_the_token_tracks_the_reported_packet_count(self):
        self.probe.consume_payload(record(0x1234, 5, 500))
        self.assertEqual(self.probe(self.entry), 5)
        self.probe.consume_payload(record(0x1234, 9, 900))
        self.assertEqual(self.probe(self.entry), 9)

    def test_other_flows_do_not_answer_for_this_one(self):
        self.probe.consume_payload(record(0x9999, 77, 7700))
        self.assertIsNone(self.probe(self.entry))

    def test_forget_drops_a_flow(self):
        self.probe.consume_payload(record(0x1234, 5, 500))
        self.probe.forget(0x1234)
        self.assertIsNone(self.probe(self.entry))

    def test_a_whole_message_is_consumed_past_its_header(self):
        taken = self.probe.consume_message(
            header() + record(0x1234, 7, 700) + record(0x9999, 1, 100))
        self.assertEqual(taken, 2)
        self.assertEqual(self.probe(self.entry), 7)

    def test_other_messages_are_ignored_without_decoding(self):
        """Including ones whose payload is not records at all."""
        for msg in (header(msg_type=MSG_TYPE_FLOWSTATS),
                    header(code=CTRL_CODE_SESS_AGEOUT),
                    header(msg_type=0) + b'not-a-record'):
            self.assertEqual(self.probe.consume_message(msg), 0)
        self.assertEqual(self.probe.ignored, 3)
        self.assertEqual(self.probe.records, 0)
        self.assertIsNone(self.probe(self.entry))


class WithAging(unittest.TestCase):
    """The probe must satisfy the contract ffn_fe100_aging relies on."""

    class Manager:
        recovery_required = False
        def __init__(self): self.sessions = {}
        def remove(self, ident): self.sessions.pop(ident)

    def setUp(self):
        self.clock = [1000.0]
        self.probe = FS.FlowStatsProbe()
        self.manager = self.Manager()
        self.aging = SessionAging(self.manager, self.probe,
                                  clock=lambda: self.clock[0])
        fwd = key4('192.0.2.10', '198.51.100.7', 1024, 53, 17, 4094)
        rev = key4('198.51.100.7', '192.0.2.10', 53, 1024, 17, 4094)
        self.entries = (forwarding_entry4(fwd, 0x10, 31),
                        forwarding_entry4(rev, 0x11, 31))
        self.manager.sessions[1] = {'entries': self.entries, 'revision': 1,
                                    'state': 'installed'}

    def report(self, packets):
        self.probe.consume_message(
            header() + record(0x10, packets, packets * 100)
            + record(0x11, packets, packets * 100))

    def test_a_session_with_no_statistics_yet_is_never_expired(self):
        self.aging.sweep()
        self.clock[0] += 100000
        out = self.aging.sweep()
        self.assertEqual(out['expired'], [])
        self.assertEqual(out['unreadable'], 1)

    def test_a_reported_then_silent_session_expires(self):
        self.report(1)
        self.aging.sweep()
        self.clock[0] += 31                      # UDP timeout is 30 s
        self.assertEqual(self.aging.sweep()['expired'], [1])

    def test_continuing_traffic_keeps_it(self):
        for n in range(1, 6):
            self.report(n)
            self.aging.sweep()
            self.clock[0] += 20
        self.assertEqual(self.aging.sweep()['expired'], [])

    def test_a_wrapping_packet_counter_is_activity(self):
        self.report((1 << 30) - 1)
        self.aging.sweep()
        self.clock[0] += 20
        self.report(0)                            # wrapped
        self.aging.sweep()
        self.clock[0] += 20
        self.assertEqual(self.aging.sweep()['expired'], [])

    def test_ignored_messages_do_not_keep_a_session_alive(self):
        """A stream of non-statistics traffic must not read as activity."""
        self.report(1)
        self.aging.sweep()
        for _ in range(2):
            self.clock[0] += 10
            self.probe.consume_message(header(code=CTRL_CODE_SESS_AGEOUT))
            self.assertEqual(self.aging.sweep()['expired'], [])
        self.assertEqual(self.probe.ignored, 2)
        self.clock[0] += 11                       # 31 s since the last report
        self.probe.consume_message(header(code=CTRL_CODE_SESS_AGEOUT))
        self.assertEqual(self.aging.sweep()['expired'], [1])


if __name__ == '__main__':
    unittest.main(verbosity=2)
