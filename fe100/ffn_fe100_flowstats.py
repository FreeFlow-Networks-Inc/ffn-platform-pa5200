#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""Per-flow activity from the FE100's own statistics messages.

This is the activity source ffn_fe100_aging needs, and it is a PUSH, not a
poll. The earlier assumption -- that idleness would be read from a counter
somewhere -- was wrong twice over, and both dead ends are worth recording
because each looks reasonable until you check:

  * `backend.fetch()` cannot work. The 64-byte wire entry is byte-stable;
    install() asserts the readback equals what was written, so hardware
    demonstrably writes nothing into that view.
  * SEM's register block cannot work either. 0x78000-0x78818 is 269 registers
    of aggregate status -- free-list levels, error logs, block throughput --
    with no "read counter N" anywhere. The counters live in FCM's external
    DDR, and FCM's own block is a memory controller: BIST, request FIFOs,
    latency. Polling either would mean issuing DDR reads on the packet path's
    own memory.

The FE100 already solves this. It emits a CPU message carrying per-flow packet
and octet totals, which is how the vendor collects session statistics:

    MSG_TYPE_FLOWSTATS = 19          (fe100ToOcteonHdr.type, byte 3)

carrying one or more 16-byte `sessionCount` records. From the vendor's own
pcs/packets/fe100.py, the layout is bit-packed and splits exactly on the
eight-byte boundary, which is what makes decoding it two shifts rather than a
bit-walker:

    word0   flow_idx : 32 | pad : 2 | packets : 30
    word1   pad : 22      | octets : 42

`flow_idx` is the flow ID -- the same value SessionManager already writes into
every entry at bytes [36:40] via entry4(). So nothing new has to be recorded at
install time to correlate a message with a session, which was the gap that made
this look expensive.

WHY AN UNSEEN FLOW IS NOT ZERO

The probe returns None for a flow it has never received statistics for, never
0. Zero is a perfectly good activity token: returning it would start the idle
clock on a session whose first statistics message simply has not arrived yet,
and the session would be torn down on schedule while carrying traffic. None
means "cannot read", which ffn_fe100_aging treats as a reason to keep the
session. The distinction is the whole safety property.

Counters are compared, never differenced, so the 30-bit packet field wrapping
is activity like any other change.

NOT YET QUALIFIED ON HARDWARE. The decoder is derived from the vendor's message
definition and is unit tested; no FLOWSTATS message has been captured from this
appliance. Before aging is enabled in production, confirm that the FE100 is
configured to emit these messages at all, and at what interval -- an emission
period longer than the shortest idle timeout would expire live sessions.
"""
import struct

MSG_TYPE_FLOWSTATS = 19          # fe100ToOcteonHdr.type
TYPE_OFFSET = 3                  # flags:8, err:8, ssp:8, type:8
RECORD_SIZE = 16

PACKETS_MASK = (1 << 30) - 1
OCTETS_MASK = (1 << 42) - 1

FLOW_ID_SLICE = slice(36, 40)    # entry4(): key(16) + 20 + flow_id(4) + 24


def decode_record(data):
    """One 16-byte sessionCount record -> (flow_id, packets, octets)."""
    if len(data) != RECORD_SIZE:
        raise ValueError('a sessionCount record is %d bytes' % RECORD_SIZE)
    word0, word1 = struct.unpack('!QQ', data)
    return (word0 >> 32, word0 & PACKETS_MASK, word1 & OCTETS_MASK)


def decode_records(payload):
    """Every whole record in a FLOWSTATS payload.

    A trailing partial record is a truncated message, not a record to guess at.
    """
    if len(payload) % RECORD_SIZE:
        raise ValueError('FLOWSTATS payload is not a whole number of records')
    return [decode_record(payload[i:i + RECORD_SIZE])
            for i in range(0, len(payload), RECORD_SIZE)]


def flow_id_of(entry):
    """The flow ID SessionManager wrote into a flow entry."""
    return int.from_bytes(bytes(entry)[FLOW_ID_SLICE], 'big')


class FlowStatsProbe:
    """Activity tokens for ffn_fe100_aging, fed by FLOWSTATS messages.

    Owner feeds each FE100 CPU message in; aging calls the instance. Both run
    under the same single-owner serialisation as SessionManager.
    """

    def __init__(self):
        self.packets = {}            # flow_id -> last reported packet count
        self.octets = {}
        self.messages = 0
        self.records = 0
        self.ignored = 0             # messages that were not FLOWSTATS

    def consume_message(self, message):
        """Feed one FE100-to-Octeon message; non-FLOWSTATS types are ignored.

        Returns the number of records taken, so a caller can tell "no stats in
        this message" from "this message was not stats at all".
        """
        message = bytes(message)
        if len(message) <= TYPE_OFFSET:
            raise ValueError('message is shorter than its header')
        if message[TYPE_OFFSET] != MSG_TYPE_FLOWSTATS:
            self.ignored += 1
            return 0
        return self.consume_payload(message[header_length(message):])

    def consume_payload(self, payload):
        """Feed the record area of a FLOWSTATS message."""
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


def header_length(_message):
    """Bytes of fe100ToOcteonHdr preceding the record area.

    NOT DERIVED FROM CAPTURED TRAFFIC. The vendor header carries several
    optional sections and the observed CPU header has been reported at both 24
    and 32 bytes in different paths, so the correct value here has to come from
    a real FLOWSTATS message rather than from counting fields in
    pcs/packets/fe100.py. Callers that already know the boundary should use
    consume_payload() and bypass this.
    """
    raise NotImplementedError(
        'FLOWSTATS header length is unverified; capture one message from the '
        'appliance and fix this, or call consume_payload() with the record '
        'area you have already separated.')
