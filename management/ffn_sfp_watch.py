#!/usr/bin/env python3
"""Re-link a faceplate port when a transceiver is inserted. Runs on the CP.

The configuration applier programs each port's link speed at commit time
through a recipe chosen for the module present then (1000BASE-X with Clause 37
for 1 Gb/s optics, XFI for 10 Gb/s optics, the generic SDK path otherwise).
A module inserted later would keep the previous mode. This service watches cage
presence on the PCA9555 expander and, when a module appears in a port that is
MAC-enabled with a configured speed, re-applies that speed, so the mode follows
the module present now. It changes no configuration, acts only on insertion
(never on what is already present when it starts: the applier owns that), and
leaves ports with a pending faceplate operation or a running aggregate owner
alone. Each action is journaled as one JSON line.
"""
import fcntl
import json
import sys
import time
sys.path.insert(0, '/usr/local/sbin')

INTERVAL = 5.0          # presence poll: two PCA9555 reads share bus 1 with the thermal governor
SETTLE = 1.0            # a fresh module's EEPROM needs a moment before it answers
IDENTITY_DEADLINE = 30  # give up on a module whose identity never validates
BACKOFF = 60.0          # after a bus timeout, leave the bus alone for this long
CHECK_INTERVAL = 60.0   # transmit verification: one SFF-8472 diagnostics read per present module
LOCK = '/run/ffn-faceplate.lock'


def presence(inventory):
    return {port: bool(row.get('present')) for port, row in inventory().items()}


def relink_locked(port, relink, lock_path=LOCK):
    with open(lock_path, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return relink(port)


class Watcher:
    """Insertion tracking: absent -> present starts a pending re-link that
    waits for a readable identity, then runs once."""

    def __init__(self, inventory, identity, relink, clock=time.monotonic, log=print, check=None, remedies=None,
                 check_interval=CHECK_INTERVAL):
        self.inventory, self.identity, self.relink = inventory, identity, relink
        self.clock, self.log = clock, log
        self.previous = None
        self.pending = {}
        self.quiet_until = 0.0
        # check(present) -> per-port results; remedies maps a remedy name to a
        # callable taking the result. Verdict changes are journaled; a remedy
        # runs at most once per verdict occurrence, never on every poll.
        self.check, self.remedies, self.check_interval = check, remedies or {}, check_interval
        self.next_check = 0.0
        self.verdicts = {}

    def emit(self, **event):
        self.log(json.dumps(event, sort_keys=True, default=str))

    def poll(self):
        now = self.clock()
        if now < self.quiet_until:
            return
        try:
            present = presence(self.inventory)
        except (OSError, RuntimeError, ValueError) as error:
            # A timeout means the bus or a device is wedged; polling into it
            # only adds traffic. Back off and let the governor and operators
            # see a quiet bus.
            self.quiet_until = now + BACKOFF
            self.emit(event='presence-unavailable', error=str(error)[:200], backoff_seconds=BACKOFF)
            return
        if self.previous is not None:
            for port in sorted(present):
                if present[port] and not self.previous.get(port, False):
                    self.pending[port] = now + IDENTITY_DEADLINE
                    self.emit(event='inserted', port=port)
        for port in list(self.pending):
            if not present.get(port):
                self.pending.pop(port)
                self.emit(event='removed-before-relink', port=port)
                continue
            if now < self.pending[port] - IDENTITY_DEADLINE + SETTLE:
                continue
            try:
                self.identity(port)
            except (OSError, ValueError) as error:
                if now >= self.pending[port]:
                    self.pending.pop(port)
                    self.emit(event='identity-unreadable', port=port, error=str(error)[:200])
                continue
            self.pending.pop(port)
            try:
                result = self.relink(port)
            except Exception as error:
                result = dict(port=port, result='error', error=str(error)[:300])
            self.emit(event='relink', **result)
        self.previous = present
        if self.check is not None and now >= self.next_check and not self.pending:
            self.next_check = now + self.check_interval
            self.verify(present, now)

    def verify(self, present, now):
        """The transmit check over the cages that hold a module; journal changes, apply local remedies."""
        try:
            results = self.check(present)
        except (OSError, RuntimeError, ValueError) as error:
            self.quiet_until = now + BACKOFF
            self.emit(event='check-unavailable', error=str(error)[:200], backoff_seconds=BACKOFF)
            return
        for result in results:
            port, verdict = result['port'], result['verdict']
            changed = self.verdicts.get(port) != verdict
            self.verdicts[port] = verdict
            if changed and verdict not in ('no-module', 'disabled'):
                self.emit(event='transmit', port=port, verdict=verdict, detail=result.get('detail', ''), remedy=result.get('remedy'),
                          tx_power_dbm=(result.get('diagnostics') or {}).get('tx_power_dbm'),
                          rx_power_dbm=(result.get('diagnostics') or {}).get('rx_power_dbm'))
            remedy = self.remedies.get(result.get('remedy'))
            if changed and remedy is not None:
                try:
                    outcome = remedy(result)
                except Exception as error:
                    outcome = dict(result='error', error=str(error)[:300])
                self.emit(event='remediated', port=port, remedy=result['remedy'], **(outcome if isinstance(outcome, dict) else dict(result=outcome)))
                self.verdicts.pop(port, None)   # re-judge on the next check rather than trusting the remedy


def main():
    import ffn_faceplate as faceplate
    import ffn_sfp_control as sfp
    import ffn_sfp_check as sfpcheck

    def check(present):
        results = sfpcheck.collect(tuple(sorted(p for p in sfpcheck.PORTS if present.get(p))) or (), diagnostics=True)
        sfpcheck.publish(results)
        return results

    def enable_transmitter(result):
        with open(LOCK, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            faceplate.sfp_apply(result['port'], True)
        return dict(result='transmitter-enabled')

    watcher = Watcher(sfp.inventory, sfp.module_identity, lambda port: relink_locked(port, faceplate.relink),
                      check=check, remedies={'enable-transmitter': enable_transmitter,
                                             'relink': lambda result: relink_locked(result['port'], faceplate.relink)})
    while True:
        watcher.poll()
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
