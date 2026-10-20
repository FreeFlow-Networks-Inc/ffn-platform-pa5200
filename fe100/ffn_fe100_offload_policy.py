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
    """
    # TODO(human)
    raise NotImplementedError('offload admission policy')
