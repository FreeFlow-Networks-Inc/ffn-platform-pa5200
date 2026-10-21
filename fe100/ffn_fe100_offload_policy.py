#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""Which observed sessions the FE100 should carry, and when.

The production offload path admits sessions from the DP observation feed: ingress
through the chip, misses and TCP control segments back to the DP, a conntrack lease
per admitted session for accounting and liveness. Everything on that path is
mechanical except one question, answered here: which candidate rows deserve a
hardware slot right now.

Why it is a policy and not a rule of thumb: the chip's commissioned session table
is small next to the LAN's conntrack table (thousands of rows, most of them short
DNS and HTTP exchanges that end before any admission could pay for itself), and
every admission costs CP table writes, a DP lease, and later a withdrawal. A
speedtest's handful of long, fat TCP flows is the ideal customer; a burst of
one-packet UDP lookups is the worst.

`should_offload` sees one compact inventory row at a time (the same rows the
Session Browser shows, see management/session_inventory.py) and a small summary
of the hardware's state. It returns True to admit the row now, False to leave it
on the software path for this pass; a row refused now is reconsidered on the next
pass, so "not yet" is a valid answer.
"""

# Hard limits the admission loop enforces regardless of the policy's answer; the
# policy may be stricter, never looser.
MAX_HARDWARE_SESSIONS = 1024        # commissioned session-table slots this build may use
PROTOCOLS = {6, 17}                 # TCP and UDP are the only qualified protocols

# The default policy's thresholds. A flow earns a slot by living long enough to
# have paid back the admission (table writes, a DP lease, a later withdrawal) and
# by carrying enough traffic that software forwarding is what it costs. The
# scarcity bar keeps the last part of the table for flows that are clearly
# worth it, so a burst of medium flows cannot crowd out a later speedtest.
MIN_AGE_SECONDS = 5.0               # younger flows are mostly DNS, probes and HTTP exchanges that end at once
MIN_PACKETS_EACH_WAY = 50           # both directions must have carried traffic: no one-way UDP floods, no half-open TCP
MIN_BYTES = 1 << 20                 # 1 MiB total, or ...
MIN_RATE_BPS = 512 * 1000           # ... 512 kbit/s averaged over the flow's life so far (a stream still ramping up)
SCARCITY_FRACTION = 0.75            # above this share of the slots in use ...
SCARCE_MIN_BYTES = 16 << 20         # ... a flow needs 16 MiB, or
SCARCE_MIN_RATE_BPS = 4 * 1000 * 1000   # ... 4 Mbit/s, before it displaces the headroom
FAILURE_BACKOFF = 3                 # this many failed admissions in the last minute: admit nothing this pass


def _count(mapping, *keys):
    """A non-negative integer from a counters dict; missing or malformed counts read 0."""
    total = 0
    for key in keys:
        value = (mapping or {}).get(key) if isinstance(mapping, dict) else None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            continue
        total += int(value)
    return total


def admissible(row):
    """The non-negotiable gate: only a software candidate with both next hops planned,
    a qualified protocol and, for TCP, an established state can ever be admitted."""
    if not row.get('candidate') or row.get('proto') not in PROTOCOLS:
        return False
    if not (row.get('l3') or {}).get('available'):
        return False
    if row.get('proto') == 6 and row.get('state') != 3:
        return False
    return True


def should_offload(row, state):
    """Decide whether this candidate row gets a hardware slot on this pass.

    row:   compact inventory row. Relevant keys: 'proto' (6 TCP / 17 UDP), 'state'
           (TCP state number, 3 is established), 'packets' and 'bytes' (dicts with
           'original' and 'reply' counts since the session began), 'since' (the
           receiver's monotonic clock when the row first appeared), 'rule' (the
           Security rule name), 'dport' / 'xdport' (destination ports).
    state: dict with 'in_hardware' (sessions already admitted), 'slots'
           (MAX_HARDWARE_SESSIONS or the commissioned subset), 'now' (monotonic
           seconds, same clock as 'since'), 'recent_failures' (admissions that
           failed in the last minute).

    Returns True to admit now, False to leave the row in software for this pass.
    `admissible(row)` has already been checked by the caller; this function is
    only about priority and timing.

    The default policy, in the order the checks bite:
      1. a bounded table: nothing while every slot is taken;
      2. back-off: nothing while admissions have been failing (the cause is not
         in the rows, retrying only adds table churn);
      3. age: the flow has lived MIN_AGE_SECONDS, so the admission can pay back;
      4. two-way traffic: MIN_PACKETS_EACH_WAY in both directions (a one-way UDP
         stream or a half-open TCP exchange is never carried);
      5. volume: MIN_BYTES in total, or MIN_RATE_BPS averaged over its life;
      6. scarcity: past SCARCITY_FRACTION of the slots, the bar rises to
         SCARCE_MIN_BYTES or SCARCE_MIN_RATE_BPS.
    Every input is read defensively: a malformed row is simply not admitted.
    """
    slots = state.get('slots')
    in_hardware = state.get('in_hardware') or 0
    if not isinstance(slots, int) or slots <= 0 or not isinstance(in_hardware, int) or in_hardware >= slots:
        return False
    failures = state.get('recent_failures') or 0
    if isinstance(failures, int) and failures >= FAILURE_BACKOFF:
        return False
    now, since = state.get('now'), row.get('since')
    if not isinstance(now, (int, float)) or not isinstance(since, (int, float)) or isinstance(now, bool) or isinstance(since, bool):
        return False
    age = now - since
    if age < MIN_AGE_SECONDS:
        return False
    packets = row.get('packets')
    if _count(packets, 'original') < MIN_PACKETS_EACH_WAY or _count(packets, 'reply') < MIN_PACKETS_EACH_WAY:
        return False
    volume = _count(row.get('bytes'), 'original', 'reply')
    rate = volume * 8 / age
    if in_hardware >= slots * SCARCITY_FRACTION:
        return volume >= SCARCE_MIN_BYTES or rate >= SCARCE_MIN_RATE_BPS
    return volume >= MIN_BYTES or rate >= MIN_RATE_BPS
