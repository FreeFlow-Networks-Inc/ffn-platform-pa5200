#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""The admission loop: from the supervised observation inventory to session leases.

Everything between the DP's observation feed and a session in the FE100 existed
as separate owners: the CP control service holds the ordered inventory
(`Observations`), the policy owner installs paired entries (`PolicyOwner`), the
lifecycle keeps bounded leases and revokes them (`SessionLifecycle`), and the
offload policy says which rows deserve a slot (`ffn_fe100_offload_policy`).
Nothing drove them together: the evaluator reported what blocked each row and
installed nothing. This loop is the driver.

Two modes. `observe` runs the gate and the policy over the live inventory and
keeps a shadow lease table with the lifecycle's own idle and maximum lifetimes,
so the counters show what the policy would have put in hardware, renewed and
withdrawn, on real traffic, before any table is written. `admit` drives a real
`SessionLifecycle`: it starts the producer lease on the owner's barrier, sends
one open event per admitted row with the paired request the dry-run evaluator
already builds, refreshes a lease while its row keeps moving, closes it when the
row closes, and heartbeats every pass. Admission failures are counted into the
policy's back-off. `off` does nothing.

The loop never chooses hardware indices, infers a verdict or allocates a flow
ID: those stay with the owners it calls. A pass is bounded work and runs under
the control service's own locks on its tick.
"""
import collections
import time

from ffn_fe100_offload_policy import admissible, should_offload, MAX_HARDWARE_SESSIONS
from ffn_fe100_policy import digest

MODES = ('off', 'observe', 'admit')
IDLE_TIMEOUT = 60           # the lifecycle's idle lease: a row with no new traffic for this long is withdrawn
MAXIMUM_LIFETIME = 300      # the lifecycle's hard lease: every admission is reconsidered after this long
FAILURE_WINDOW = 60         # admissions that failed within this window feed the policy's back-off
FAILURE_RETRY = 30          # a row whose admission failed is not retried before this
PASS_BUDGET = 256           # rows examined for admission per pass; the cursor rotates so every row gets its turn
TOP_REASONS = 8


def count(counters, direction, key):
    """A non-negative integer from the stream row's counters, 0 when absent or malformed."""
    value = ((counters or {}).get(direction) or {}).get(key) if isinstance(counters, dict) else None
    return value if type(value) is int and value >= 0 else 0


def compact(row, since):
    """The policy's view of a stream row: protocol, state, both directions' counts,
    first-seen clock, rule and ports. Missing fields read as absent, never as values."""
    original = row.get('original') if isinstance(row.get('original'), dict) else {}
    translated = row.get('translated') if isinstance(row.get('translated'), dict) else {}
    rule = row.get('rule') if isinstance(row.get('rule'), dict) else {}
    counters = row.get('counters')
    return dict(proto=original.get('protocol'), state=row.get('tcp_state'),
                candidate=row.get('software_candidate') is True,
                l3=row.get('l3') if isinstance(row.get('l3'), dict) else {},
                packets=dict(original=count(counters, 'original', 'packets'), reply=count(counters, 'reply', 'packets')),
                bytes=dict(original=count(counters, 'original', 'bytes'), reply=count(counters, 'reply', 'bytes')),
                since=since, rule=rule.get('name'), dport=original.get('destination_port'),
                xdport=translated.get('destination_port'))


def traffic(row):
    """The row's packet total, the signal that a leased session is still moving."""
    counters = row.get('counters')
    return count(counters, 'original', 'packets') + count(counters, 'reply', 'packets')


class AdmissionLoop:
    def __init__(self, mode='observe', *, policy=should_offload, gate=admissible, slots=MAX_HARDWARE_SESSIONS,
                 clock=time.monotonic, lifecycle=None, request=None, log=lambda line: None):
        if mode not in MODES: raise ValueError('admission loop mode must be one of %s' % (MODES,))
        if mode == 'admit' and (lifecycle is None or request is None):
            raise ValueError('admit mode needs a session lifecycle and a request builder')
        if type(slots) is not int or not 1 <= slots <= MAX_HARDWARE_SESSIONS:
            raise ValueError('slots must be 1..%d' % MAX_HARDWARE_SESSIONS)
        self.mode, self.policy, self.gate, self.slots, self.clock = mode, policy, gate, slots, clock
        self.lifecycle, self.request, self.log = lifecycle, request, log
        self.leases = {}            # identity -> {'session_id','since','expires','deadline','traffic'[,'decision']}
        self.quiet = {}             # identity -> packet total at its idle withdrawal; not re-admitted until it moves
        self.failures = {}          # identity -> clock of the last failed admission
        self.failed_at = []         # clocks of failed admissions, for the policy's back-off
        self.producer = None        # the producer the lifecycle lease was started for (admit mode)
        self.sequence = 0           # the lifecycle's event sequence for that producer
        self.cursor = None          # the identity the next pass resumes after
        self.counters = collections.Counter()
        self.last = dict(at=None, examined=0, reasons={})

    # ---- bookkeeping -----------------------------------------------------
    def recent_failures(self, now):
        self.failed_at = [at for at in self.failed_at if now - at < FAILURE_WINDOW]
        return len(self.failed_at)

    def state(self, now):
        return dict(in_hardware=len(self.leases), slots=self.slots, now=now, recent_failures=self.recent_failures(now))

    def fail(self, identity, now, reason):
        self.counters['failed'] += 1
        self.failures[identity] = now
        self.failed_at.append(now)
        self.log('admission of %s failed: %s' % (identity[:12], str(reason)[:200]))

    # ---- the lifecycle, admit mode only ---------------------------------
    def event(self, operation, payload):
        """One ordered lifecycle event; the loop owns the sequence. Raises what the lifecycle raises."""
        self.lifecycle.event(self.producer, self.sequence + 1, operation, payload)
        self.sequence += 1

    def synchronize(self, receiver, owner):
        """Start (or restart) the producer lease when the inventory's producer or the owner's barrier changed."""
        producer = getattr(receiver, 'producer', None)
        if not isinstance(producer, dict) or not isinstance(owner, dict): return False
        if self.producer == producer and self.lifecycle.status().get('synchronized'): return True
        try:
            self.lifecycle.start(dict(producer), owner.get('revision'), owner.get('digest'))
        except Exception as error:
            self.producer = None; self.leases.clear()
            self.log('producer lease not started: %s' % str(error)[:200]); return False
        self.producer, self.sequence = dict(producer), 0
        self.leases.clear(); self.counters['synchronized'] += 1
        return True

    def desynchronize(self, reason):
        if self.producer is not None:
            try: self.lifecycle.fence(reason)
            except Exception as error: self.log('fence failed: %s' % str(error)[:200])
        self.producer = None; self.sequence = 0; self.leases.clear()

    # ---- leases ----------------------------------------------------------
    def open(self, identity, row, now, receiver, owner):
        lease = dict(session_id=row.get('conntrack_id'), since=now, expires=now + IDLE_TIMEOUT,
                     deadline=now + MAXIMUM_LIFETIME, traffic=traffic(row))
        if self.mode == 'admit':
            try:
                payload = self.request(row, receiver, owner)
                self.event('open', payload)
                lease['session_id'] = payload.get('session_id')
                lease['decision'] = digest(payload)     # what the lifecycle's refresh must quote back
            except Exception as error:
                self.fail(identity, now, error)
                if self.lifecycle.status().get('synchronized') is not True: self.desynchronize('open failed')
                return False
        self.leases[identity] = lease
        self.counters['admitted'] += 1
        return True

    def close(self, identity, now, reason):
        lease = self.leases.pop(identity, None)
        if lease is None: return
        self.counters['closed'] += 1; self.counters['closed:' + reason] += 1
        # An idle row still passes the policy on its lifetime volume; it is not
        # re-admitted until it carries another packet.
        if reason == 'idle': self.quiet[identity] = lease['traffic']
        if self.mode == 'admit':
            try: self.event('close', dict(session_id=lease['session_id']))
            except Exception as error:
                self.log('close of %s failed: %s' % (identity[:12], str(error)[:200]))
                self.desynchronize('close failed')

    def refresh(self, identity, lease, now):
        lease['expires'] = min(now + IDLE_TIMEOUT, lease['deadline'])
        self.counters['renewed'] += 1
        if self.mode == 'admit':
            try: self.event('refresh', dict(session_id=lease['session_id'], decision_digest=lease['decision']))
            except Exception as error:
                self.log('refresh of %s failed: %s' % (identity[:12], str(error)[:200]))
                self.leases.pop(identity, None); self.desynchronize('refresh failed')

    # ---- one pass --------------------------------------------------------
    def run(self, receiver, owner=None, bound=PASS_BUDGET):
        """One bounded pass over the inventory; returns the status."""
        now = self.clock(); self.counters['passes'] += 1
        reasons = collections.Counter(); examined = 0
        if self.mode == 'off':
            self.last = dict(at=now, examined=0, reasons={}); return self.status()
        ready = receiver is not None and getattr(receiver, 'ready', False) is True
        if not ready:
            for identity in list(self.leases): self.close(identity, now, 'inventory withdrawn')
            if self.mode == 'admit': self.desynchronize('inventory not ready')
            self.last = dict(at=now, examined=0, reasons={'inventory not ready': 1}); return self.status()
        if self.mode == 'admit':
            if not self.synchronize(receiver, owner):
                self.last = dict(at=now, examined=0, reasons={'producer lease not started': 1}); return self.status()
            try: self.event('heartbeat', {})
            except Exception as error:
                self.log('heartbeat failed: %s' % str(error)[:200]); self.desynchronize('heartbeat failed')
                self.last = dict(at=now, examined=0, reasons={'heartbeat failed': 1}); return self.status()
        rows, since = receiver.sessions, getattr(receiver, 'since', {})
        # Leases first: rows gone, rows no longer admissible, hard deadline, idle.
        for identity, lease in list(self.leases.items()):
            row = rows.get(identity)
            if row is None: self.close(identity, now, 'row closed'); continue
            if not self.gate(compact(row, since.get(identity, now))): self.close(identity, now, 'no longer admissible'); continue
            if now >= lease['deadline']: self.close(identity, now, 'maximum lifetime'); continue
            moved = traffic(row)
            if moved != lease['traffic']:
                lease['traffic'] = moved; self.refresh(identity, lease, now)
            elif now >= lease['expires']: self.close(identity, now, 'idle')
        # Then admissions, from the cursor, within the budget.
        self.quiet = {identity: moved for identity, moved in self.quiet.items() if identity in rows}
        identities = sorted(rows)
        start = 0
        if self.cursor is not None:
            start = next((i for i, ident in enumerate(identities) if ident > self.cursor), 0)
        order = identities[start:] + identities[:start]
        for identity in order:
            if examined >= bound: break
            if identity in self.leases: continue
            row = rows.get(identity)
            if row is None: continue
            examined += 1; self.cursor = identity
            prow = compact(row, since.get(identity, now))
            if not self.gate(prow): reasons['not admissible'] += 1; continue
            if identity in self.quiet:
                if self.quiet[identity] == traffic(row): reasons['idle since withdrawal'] += 1; continue
                del self.quiet[identity]
            if identity in self.failures and now - self.failures[identity] < FAILURE_RETRY:
                reasons['recent failure'] += 1; continue
            if not self.policy(prow, self.state(now)): reasons['policy: not now'] += 1; continue
            if self.open(identity, row, now, receiver, owner): reasons['admitted'] += 1
            else: reasons['admission failed'] += 1
            if self.mode == 'admit' and self.producer is None: break
        self.counters['examined'] += examined
        self.last = dict(at=now, examined=examined, reasons=dict(reasons.most_common(TOP_REASONS)))
        return self.status()

    def status(self):
        now = self.clock()
        return dict(mode=self.mode, hardware_admission=self.mode == 'admit', slots=self.slots,
                    leases=len(self.leases), recent_failures=self.recent_failures(now),
                    counters=dict(self.counters), last_pass=dict(self.last),
                    producer=dict(self.producer) if self.producer else None, sequence=self.sequence,
                    lifecycle=self.lifecycle.status() if self.lifecycle is not None else None)
