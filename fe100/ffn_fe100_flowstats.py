#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""FE100 activity sources for the control-plane session owner.

PA-5220 live capture and the sysroot condor_fcr_t layout establish that native
FLOWSTATS (19) uses eight-byte records containing a flow ID, a two-bit reason,
eight-bit packet DELTA and 22-bit octet DELTA. The C decoder in
../octeon/native/ffn_fe100_stats.c owns this packet format. NativeCounterStream
consumes only its decoded control events, tied to an explicit owner epoch and
registered hardware flow IDs. Equal consecutive deltas are separate activity.

The older CONTROL/STATS_COUNTER, 16-byte sessionCount codec below is retained
as a diagnostic reference format. It was NOT the format emitted by this device
and must not be used as the live activity source. The PCS diagnostic demux is
not evidence that FLOWSTATS is unused.

Neither decoder alone enables production offload. A complete receiver must
report loss and feed the current single session owner; software conntrack/NAT
refresh and hardware withdrawal on receiver loss still require commissioning.
"""
import struct
import time
import math

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


class NativeCounterStream:
    """Single-owner accounting of decoded native deltas, with no packet I/O.

    The control transport supplies the epoch; a packet cannot choose it. The
    epoch must identify the hardware/table generation, not just a receiver PID.
    Retired IDs cannot be reused in that generation: a late report has no tuple
    or generation field with which to distinguish a newly allocated session.
    Any sequence gap, malformed event or capture failure latches unavailable.
    The coordinator must withdraw hardware flows when available becomes false.
    This class never treats receiver heartbeats as flow activity.
    """
    def __init__(self, epoch, *, clock=time.monotonic, freshness=5, max_ids=65536):
        if not isinstance(epoch, str) or not epoch or len(epoch)>256:
            raise ValueError('explicit hardware owner epoch required')
        if type(freshness) not in (int,float) or not 0<freshness<=60:
            raise ValueError('invalid activity freshness')
        if type(max_ids) is not int or not 1<=max_ids<=1048576:
            raise ValueError('invalid flow ID budget')
        self.epoch,self.clock,self.freshness,self.max_ids=epoch,clock,freshness,max_ids
        self.sequence=0
        self.elapsed_ms=0
        self.last_clock=None
        self.available=True
        self.failure=None
        self.flows={}
        self.retired=set()
        self.ignored=0

    def invalidate(self, reason):
        self.available=False
        self.failure=str(reason)

    def now(self):
        value=self.clock()
        if (type(value) not in (int,float) or not math.isfinite(value) or value<0 or
                self.last_clock is not None and value<self.last_clock):
            self.invalidate('invalid or regressed counter clock')
            raise ValueError(self.failure)
        self.last_clock=value
        return value

    def register(self, entry):
        from ffn_fe100_sessions import validate_entry4
        wire=validate_entry4(entry)
        ident=flow_id_of(wire)
        if not self.available:
            raise RuntimeError('counter stream unavailable')
        if not ident or ident in self.retired:
            raise ValueError('flow ID cannot be reused in this hardware epoch')
        if ident in self.flows:
            if self.flows[ident]['entry']!=wire:
                raise ValueError('flow ID already belongs to another entry')
            return
        if len(self.flows)+len(self.retired)>=self.max_ids:
            raise RuntimeError('flow ID generation budget exhausted')
        self.flows[ident]={'entry':wire,'packets':0,'octets':0,
                           'activity':0,'observed':False,'at':None}

    def forget(self, flow_id):
        if flow_id in self.flows:
            del self.flows[flow_id]
            self.retired.add(flow_id)

    @staticmethod
    def _uint(value, maximum):
        return type(value) is int and 0<=value<=maximum

    def consume(self, epoch, event):
        if not self.available:
            raise RuntimeError('counter stream unavailable: '+str(self.failure))
        try:
            if epoch!=self.epoch:
                raise ValueError('hardware owner epoch changed')
            if not isinstance(event,dict) or set(event)!={'sequence','elapsed_ms','records'}:
                raise ValueError('invalid native counter event')
            seq=event['sequence'];records=event['records']
            if not self._uint(seq,(1<<64)-1) or seq!=self.sequence+1:
                raise ValueError('counter stream sequence gap or replay')
            if not self._uint(event['elapsed_ms'],(1<<64)-1) or event['elapsed_ms']<self.elapsed_ms:
                raise ValueError('invalid receiver time')
            if not isinstance(records,list) or not 1<=len(records)<=125:
                raise ValueError('invalid counter record count')
            # Validate the whole message before changing any accumulated value.
            for record in records:
                if not isinstance(record,dict) or set(record)!={'flow_id','packets','octets','reason'}:
                    raise ValueError('invalid native counter record')
                for key,limit in (('flow_id',0xffffffff),('packets',255),('octets',0x3fffff),('reason',3)):
                    if not self._uint(record[key],limit):
                        raise ValueError('invalid '+key)
            now=self.now()
            for record in records:
                flow=self.flows.get(record['flow_id'])
                if flow is None:
                    self.ignored+=1
                    continue
                flow['packets']+=record['packets']
                flow['octets']+=record['octets']
                # The reference receive path accounts reason=2 counters but
                # skips its activity refresh. Do not extend a NAT lease on it.
                if record['packets'] and record['reason']!=2:
                    flow['activity']+=1
                    flow['observed']=True
                    flow['at']=now
            self.sequence=seq
            self.elapsed_ms=event['elapsed_ms']
        except (ValueError,TypeError) as exc:
            self.invalidate(exc)
            raise

    def receiver_finished(self, summary):
        # Even a clean bounded receiver exit ends the active accounting lease.
        if not isinstance(summary,dict) or any(summary.get(k) for k in ('failed','malformed','capture_drops')):
            self.invalidate('native counter receiver failed or lost messages')
        else:
            self.invalidate('native counter receiver stopped')

    def totals(self, entry):
        flow=self.flows.get(flow_id_of(entry))
        if flow is None or flow['entry']!=bytes(entry):
            return None
        return {'packets':flow['packets'],'octets':flow['octets']}

    def __call__(self, entry):
        flow=self.flows.get(flow_id_of(entry))
        if not self.available or flow is None or flow['entry']!=bytes(entry) or not flow['observed']:
            return None
        try:age=self.now()-flow['at']
        except ValueError:return None
        if age<0 or age>self.freshness:
            return None
        return flow['activity']


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
