#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""Per-flow activity from the FE100's own statistics messages.

This is the activity source ffn_fe100_aging needs, and it is a PUSH, not a
poll. Two other answers look reasonable until you check them:

  * `backend.fetch()` cannot work. The wire entry is byte-stable; install()
    asserts the readback equals what was written, so hardware demonstrably
    writes nothing into that view. The vendor's own `flowEntry` layout agrees:
    60 bytes of key, action, rewrite and flow ID, with no counter field.
  * SEM's register block cannot work either. 0x78000-0x78818 is 269 registers
    of aggregate status -- free-list levels, error logs, block throughput --
    with no "read counter N". The counters live in FCM's external DDR, and
    FCM's own block is a memory controller: BIST, request FIFOs, latency. The
    vendor's only counter read, `pan_fe100_get_flow_ctrs`, is likewise
    table-wide (`pan_fe100_flow_stats_t`), not per flow.

The FE100 pushes them instead, in a CPU message:

    fe100ToOcteonHdr.type == MSG_TYPE_CONTROL (0x80)
    fe100ToOcteonHdr.code == CTRL_CODE_STATS_COUNTER (5)

BOTH have to match. `MSG_TYPE_FLOWSTATS = 19` is a decoy: it is defined in the
vendor's message-type enum, it is the obvious thing to key on, and nothing in
the vendor tree ever uses it. Their own decoder demuxes sessionCount on
(CONTROL, STATS_COUNTER), so that is what the hardware emits.

The header is a FIXED 32 bytes -- 21 fields summing to exactly 256 bits, no
optional sections and no length field -- so the record area starts at byte 32
and header_length() is a constant rather than a parse.

    byte  3   type
    byte 16   flow_id      (the header's own; records carry their own index)
    byte 23   code
    byte 32   records start

Each record is a 16-byte `sessionCount`, bit-packed but splitting exactly on
the eight-byte boundary, which is what makes decoding it two shifts rather
than a bit-walker:

    word0   flow_idx : 32 | pad : 2 | packets : 30
    word1   pad : 22      | octets : 42

`flow_idx` is the flow ID -- byte 36 of the vendor's `flowEntry`, which is
where entry4() already writes it. So nothing new has to be recorded at install
time to correlate a message with a session.

The vendor's decoder attaches exactly ONE record per message (sessionCount
never chains a next layer). decode_records() accepts several because a device
that batched them would otherwise be silently truncated, and one record is the
single-record case of the same loop.

WHY AN UNSEEN FLOW IS NOT ZERO

The probe returns None for a flow it has never received statistics for, never
0. Zero is a perfectly good activity token: returning it would start the idle
clock on a session whose first statistics message simply has not arrived yet,
and the session would be torn down on schedule while carrying traffic. None
means "cannot read", which ffn_fe100_aging treats as a reason to keep the
session. The distinction is the whole safety property.

Counters are compared, never differenced, so the 30-bit packet field wrapping
is activity like any other change.

NOT YET QUALIFIED ON HARDWARE. The layout is taken from the vendor's field
definitions rather than guessed, and is unit tested -- but no message has been
captured from this appliance. Before aging is enabled in production, confirm
the FE100 is configured to emit these at all and at what interval: an emission
period longer than the shortest idle timeout would expire live sessions.

Worth knowing when that is investigated: the same control family carries
CTRL_CODE_SESS_AGEOUT (4), so the FE100 may be able to age flows itself. If it
does, this module becomes a cross-check rather than the mechanism.
"""
import struct

MSG_TYPE_CONTROL = 0x80          # fe100ToOcteonHdr.type
CTRL_CODE_STATS_COUNTER = 5      # fe100ToOcteonHdr.code

TYPE_OFFSET = 3                  # flags:8, err:8, ssp:8, type:8
CODE_OFFSET = 23                 # ... res:16, rec:1, icode:7, code:8
HEADER_SIZE = 32                 # 21 fields, 256 bits, fixed

RECORD_SIZE = 16
PACKETS_MASK = (1 << 30) - 1
OCTETS_MASK = (1 << 42) - 1

FLOW_ID_SLICE = slice(36, 40)    # flowEntry.flowid, and what entry4() writes


def decode_record(data):
    """One 16-byte sessionCount record -> (flow_id, packets, octets)."""
    if len(data) != RECORD_SIZE:
        raise ValueError('a sessionCount record is %d bytes' % RECORD_SIZE)
    word0, word1 = struct.unpack('!QQ', data)
    return (word0 >> 32, word0 & PACKETS_MASK, word1 & OCTETS_MASK)


def decode_records(payload):
    """Every whole record in a statistics payload.

    A trailing partial record is a truncated message, not a record to guess at.
    """
    if len(payload) % RECORD_SIZE:
        raise ValueError('statistics payload is not a whole number of records')
    return [decode_record(payload[i:i + RECORD_SIZE])
            for i in range(0, len(payload), RECORD_SIZE)]


def header_length(message):
    """Bytes of fe100ToOcteonHdr preceding the record area.

    Constant, because the header is: 21 fixed-width fields, no optional
    sections and nothing to parse. It still takes the message so that the
    length check happens once here rather than at every call site, and so a
    future header variant would be distinguished in one place.
    """
    if len(message) < HEADER_SIZE:
        raise ValueError('message is shorter than its %d-byte header'
                         % HEADER_SIZE)
    return HEADER_SIZE


def is_stats_message(message):
    """True for the CONTROL/STATS_COUNTER messages that carry sessionCount.

    Both fields are checked. CONTROL alone also carries session setup, update,
    remove and ageout, whose payloads are not records.
    """
    if len(message) < HEADER_SIZE:
        raise ValueError('message is shorter than its %d-byte header'
                         % HEADER_SIZE)
    return (message[TYPE_OFFSET] == MSG_TYPE_CONTROL
            and message[CODE_OFFSET] == CTRL_CODE_STATS_COUNTER)


def flow_id_of(entry):
    """The flow ID SessionManager wrote into a flow entry."""
    return int.from_bytes(bytes(entry)[FLOW_ID_SLICE], 'big')


class FlowStatsProbe:
    """Activity tokens for ffn_fe100_aging, fed by statistics messages.

    Owner feeds each FE100 CPU message in; aging calls the instance. Both run
    under the same single-owner serialisation as SessionManager.
    """

    def __init__(self):
        self.packets = {}            # flow_id -> last reported packet count
        self.octets = {}
        self.messages = 0
        self.records = 0
        self.ignored = 0             # messages that were not statistics

    def consume_message(self, message):
        """Feed one FE100-to-Octeon message; other types are ignored.

        Returns the number of records taken, so a caller can tell "no stats in
        this message" from "this message was not stats at all".
        """
        message = bytes(message)
        if not is_stats_message(message):
            self.ignored += 1
            return 0
        return self.consume_payload(message[header_length(message):])

    def consume_payload(self, payload):
        """Feed the record area of a statistics message."""
        records = decode_records(payload)
        for flow_id, packets, octets in records:
            self.packets[flow_id] = packets
            self.octets[flow_id] = octets
        self.messages += 1
        self.records += len(records)
        return len(records)

    def forget(self, flow_id):
        """Drop a flow's statistics once its session is gone."""
        self.packets.pop(flow_id, None)
        self.octets.pop(flow_id, None)

    def __call__(self, entry):
        """ffn_fe100_aging probe: activity token, or None if never reported.

        None rather than 0 for an unknown flow -- see the module docstring.
        """
        return self.packets.get(flow_id_of(entry))
