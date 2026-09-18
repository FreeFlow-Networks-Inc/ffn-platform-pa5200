#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""Expire idle offloaded sessions, so the FE100 flow table drains.

WHY THIS HAS TO EXIST BEFORE OFFLOAD IS SWITCHED ON

Nothing ages an offloaded flow today. SessionManager installs and removes on
policy events, and that is all: a session whose traffic simply stops is
programmed into the FE100 forever. The table fills, installs start failing on a
box that looks healthy, and the only recovery is a flush nobody wants to run on
a firewall carrying traffic. Everything else about offload can be correct and it
will still not be safe to leave enabled.

WHERE ACTIVITY CANNOT COME FROM, WHICH IS THE WHOLE DESIGN PROBLEM

An offloaded packet never reaches the CPU. That is the point of offloading it.
So "when did the control plane last see this flow" is not an idleness signal --
a fully saturated offloaded session looks exactly as quiet as a dead one.

It cannot come from `backend.fetch(key)` either, which is the tempting answer
because SessionManager already has it. The 64-byte wire entry is byte-STABLE:
`install()` asserts `fetch(entry[:16]) == entry` and `validate_entry4` accepts
only the exact bytes we wrote or the pre-FLOWUPDATE identity variant. If the
hardware wrote a counter or timestamp anywhere in that view, every install would
already be failing its readback. It does not, so fetch says nothing about
traffic.

Nor from SEM's registers. 0x78000-0x78818 is 269 registers of aggregate status
-- free-list levels, error logs, block throughput -- with no "read counter N"
anywhere, because the counters live in FCM's external DDR and FCM's own block
is a memory controller (BIST, request FIFOs, latency).

The FE100 pushes them instead, as MSG_TYPE_FLOWSTATS CPU messages carrying
per-flow packet and octet totals keyed by flow_idx -- which is the flow ID
SessionManager already writes into every entry. `ffn_fe100_flowstats` decodes
those and implements this probe; see its docstring for the layout and for what
is still unverified about the message header.

    probe(entry) -> hashable token, or None when activity cannot be read

The probe stays an injected interface rather than a hard dependency, so the
policy below can be tested without a message source and a different activity
source could be substituted.

Whatever the eventual implementation, it must return a token that CHANGES when
the flow forwards a packet. A counter, a timestamp, anything comparable. The
engine never subtracts tokens, only compares them, so a counter that wraps still
reads as activity rather than as idleness.

FAILING SAFE MEANS KEEPING THE SESSION

Every ambiguity resolves toward not expiring:

  * a probe returning None (cannot read) refreshes the timer rather than
    counting as silence -- a transient read failure must never tear down live
    traffic;
  * a session is idle only when BOTH directions are unchanged, because a
    one-way flow is still a live flow;
  * a session first seen this sweep starts its clock now, so nothing is expired
    on the strength of a single observation;
  * removals per sweep are capped, because remove() takes the table lock and a
    large expiry burst must not stall installs;
  * nothing is expired while the manager wants recovery, or in any state other
    than 'installed'.

The cost of being wrong in the other direction is a broken connection on a
firewall, which is worse than a flow entry living a few sweeps too long.
"""
import struct
import time

# Seconds of silence before a session is torn down, by IP protocol. These match
# PAN-OS's defaults for the two protocols key4 admits; there is no general
# default because key4 rejects everything else.
DEFAULT_TIMEOUTS = {6: 3600, 17: 30}       # TCP, UDP

# remove() holds the FE100 table lock per session. A cap keeps one sweep from
# blocking installs behind a large expiry burst; the remainder goes next sweep.
DEFAULT_MAX_REMOVALS = 32

PROTOCOL_OFFSET = 1                        # key4: !BBHHH4s4s -> 0x40, protocol


def protocol_of(entry):
    """The IP protocol from a flow entry's key."""
    return entry[PROTOCOL_OFFSET]


class SessionAging:
    """Idle-expiry for SessionManager, driven by an external activity probe.

    Owner must call sweep() on a timer and must serialise it against other
    SessionManager calls -- SessionManager is explicitly single-owner.
    """

    def __init__(self, manager, probe, timeouts=None, clock=time.monotonic,
                 max_removals=DEFAULT_MAX_REMOVALS):
        if max_removals < 1:
            raise ValueError('max_removals must be at least 1')
        self.manager = manager
        self.probe = probe
        self.timeouts = dict(DEFAULT_TIMEOUTS if timeouts is None else timeouts)
        self.clock = clock
        self.max_removals = max_removals
        # session_id -> (token_tuple, monotonic time that token was first seen)
        self._seen = {}

    def timeout_for(self, session):
        """Idle timeout for a session, from the protocol in its first key.

        Both directional keys carry the same protocol -- install() enforces that
        they are the same tuple reversed -- so either answers.
        """
        proto = protocol_of(session['entries'][0])
        return self.timeouts.get(proto)

    def _tokens(self, session):
        """Activity tokens for both directions, or None if any is unreadable."""
        tokens = []
        for entry in session['entries']:
            token = self.probe(entry)
            if token is None:
                return None
            tokens.append(token)
        return tuple(tokens)

    def sweep(self):
        """One expiry pass. Returns a dict describing what it decided and why.

        Never raises for a single session's failure: one flow that cannot be
        removed must not stop the rest of the table from draining. A removal
        failure has already set recovery_required inside SessionManager, which
        stops the next sweep at the top.
        """
        now = self.clock()
        result = {'expired': [], 'failed': [], 'active': 0, 'unreadable': 0,
                  'idle': 0, 'deferred': 0, 'skipped': None}

        if getattr(self.manager, 'recovery_required', False):
            # Hardware ownership is in doubt; expiring now could delete a flow
            # the journal no longer describes accurately.
            result['skipped'] = 'recovery required'
            return result

        sessions = dict(self.manager.sessions)
        # Stop tracking anything policy already removed, so _seen cannot grow
        # without bound across a long uptime.
        for ident in list(self._seen):
            if ident not in sessions:
                del self._seen[ident]

        expiring = []
        for ident, session in sessions.items():
            if session.get('state') != 'installed':
                continue                       # mid-transaction; not ours to age
            timeout = self.timeout_for(session)
            if timeout is None:
                continue                       # no policy for this protocol

            tokens = self._tokens(session)
            if tokens is None:
                # Unreadable. Treat as activity: restart the clock rather than
                # accumulate silence we did not actually observe.
                self._seen[ident] = (None, now)
                result['unreadable'] += 1
                continue

            previous = self._seen.get(ident)
            if previous is None or previous[0] != tokens:
                self._seen[ident] = (tokens, now)
                result['active'] += 1
                continue

            if now - previous[1] >= timeout:
                expiring.append(ident)
            else:
                result['idle'] += 1

        # Deterministic order so a capped sweep makes progress through the table
        # instead of revisiting the same arbitrary subset.
        expiring.sort()
        result['deferred'] = max(0, len(expiring) - self.max_removals)
        for ident in expiring[:self.max_removals]:
            try:
                self.manager.remove(ident)
                self._seen.pop(ident, None)
                result['expired'].append(ident)
            except Exception as exc:
                result['failed'].append((ident, str(exc)))
                break                          # recovery_required; stop touching hardware
        return result


def fetch_probe(_backend):
    """Refuses to exist, loudly, because it would be the obvious wrong answer.

    backend.fetch() returns the 64-byte wire entry, which is byte-stable by
    construction: install() asserts the readback equals what was written. A
    probe built on it would report every session idle forever and expire the
    entire table on the first sweep after the timeout.
    """
    raise NotImplementedError(
        'backend.fetch() is byte-stable and cannot detect traffic; per-flow '
        'activity needs the SEM counter for the session, which nothing '
        'currently records. See the module docstring.')
