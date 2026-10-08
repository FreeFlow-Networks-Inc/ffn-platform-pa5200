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

INTERVAL = 2.0          # presence poll
SETTLE = 1.0            # a fresh module's EEPROM needs a moment before it answers
IDENTITY_DEADLINE = 30  # give up on a module whose identity never validates
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

    def __init__(self, inventory, identity, relink, clock=time.monotonic, log=print):
        self.inventory, self.identity, self.relink = inventory, identity, relink
        self.clock, self.log = clock, log
        self.previous = None
        self.pending = {}

    def emit(self, **event):
        self.log(json.dumps(event, sort_keys=True, default=str))

    def poll(self):
        try:
            present = presence(self.inventory)
        except (OSError, RuntimeError, ValueError) as error:
            self.emit(event='presence-unavailable', error=str(error)[:200])
            return
        now = self.clock()
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


def main():
    import ffn_faceplate as faceplate
    import ffn_sfp_control as sfp
    watcher = Watcher(sfp.inventory, sfp.module_identity, lambda port: relink_locked(port, faceplate.relink))
    while True:
        watcher.poll()
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
