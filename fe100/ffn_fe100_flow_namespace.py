#!/usr/bin/env python3
"""Commission the FE100 flow-ID namespace for the current hardware generation.

The flow id is a 32-bit tag in every session entry and every counter record;
the boot initialisation clears the tables, so a namespace belongs to one CP
boot. Commissioning, in the supervised control owner:

1. the FE100 reads initialised for this boot (readiness, in a subprocess: a
   live-sessions handle holds the exclusive table lock, which the owner must
   never keep open);
2. the range is proved on the hardware in that subprocess: two isolated
   entries carrying the first and the last id are inserted, read back and
   deleted, with the tables empty before and after;
3. the generation (boot, readiness owner, range) is journaled and the
   allocator is created with a digest over it. The same boot resumes the
   journal (ids are never reused); a new boot rolls the namespace over and
   archives the previous generation.

The range starts above every fixed benchmark id the isolated validators keep.
Nothing here enables admission: the policy owner still requires activation.
"""
import hashlib
import json
import os
import subprocess
import sys
import time

FIRST, LAST = 0x10000, 0xFFFFFFFE
RETRY_SECONDS = 60
PROBE_TIMEOUT = 90
# Isolated benchmark keys (RFC 2544 space, lab zone), distinct from the validators' own.
PROBE_KEYS = (('198.18.2.1', '198.18.2.2', 40011, 40012, 17, 4094),
              ('198.18.2.2', '198.18.2.1', 40012, 40011, 17, 4094))


def domain_digest(boot, owner, first, last):
    return hashlib.sha256(json.dumps([boot, owner, first, last], separators=(',', ':')).encode()).hexdigest()


def probe(first=FIRST, last=LAST):
    """Run in a subprocess on the CP: readiness, then the range proof. Returns a report."""
    from ffn_fe100_live_sessions import LiveSessions
    from ffn_fe100_sessions import key4, entry4
    from ffn_fe100_session_adapter import encode_native, decode_native, SUCCESS, NOT_FOUND
    io = LiveSessions(writable=False)
    state = io.status()
    report = dict(schema=1, cp_boot_id=state.get('cp_boot_id'), owner_sha256=state.get('owner_sha256'),
                  initialized=state.get('initialized') is True, blockers=list(state.get('blockers', [])),
                  first=first, last=last, proved=False, operations=[])
    if not report['initialized']:
        return report
    if any(state['registers'].get(r) for r in ('0x40428', '0x40450')):
        report['blockers'] = ['hardware flow tables are not empty; the range proof needs empty tables']
        return report
    io.lock.close()
    io = LiveSessions(writable=True)
    wires = [entry4(key4(*PROBE_KEYS[0]), first), entry4(key4(*PROBE_KEYS[1]), last)]
    touched = []
    def call(op, wire):
        rc, native = io.call(op, encode_native(wire))
        report['operations'].append(dict(operation=op, flow_id=int.from_bytes(wire[36:40], 'big'), rc=rc))
        return rc, native
    try:
        for wire in wires:
            rc, _ = call('fetch', wire)
            if rc != NOT_FOUND:
                raise RuntimeError('probe key is not verified absent: %d' % rc)
        for wire in wires:
            touched.append(wire)
            rc, _ = call('insert', wire)
            if rc != SUCCESS:
                raise RuntimeError('probe insert failed: %d' % rc)
            rc, native = call('fetch', wire)
            if rc != SUCCESS or decode_native(native, wire[:16]) != wire:
                raise RuntimeError('probe readback mismatch: %d' % rc)
        report['proved'] = True
    except Exception as error:
        report['error'] = str(error)[:300]
    finally:
        clean = True
        for wire in reversed(touched):
            try:
                rc, native = call('fetch', wire)
                if rc == SUCCESS:
                    rc, _ = call('delete', decode_native(native, wire[:16]))
                    rc, _ = call('fetch', wire)
                clean = clean and rc == NOT_FOUND
            except Exception as error:
                clean = False
                report['cleanup_error'] = str(error)[:300]
        report['cleanup_verified'] = clean
        if not clean:
            report['proved'] = False
    return report


class FlowNamespace:
    """Owner-side commissioning of the allocator against the journal."""

    def __init__(self, db, boot, first=FIRST, last=LAST, clock=time.monotonic, prober=None, log=None):
        self.db, self.boot, self.first, self.last = db, boot, first, last
        self.clock, self.log = clock, log or (lambda *a: None)
        self.prober = prober or self.subprocess_probe
        self.attempted = None
        self.reason = 'flow-ID namespace not commissioned yet'
        self.last_probe = None
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS flow_id_generation (id INTEGER PRIMARY KEY CHECK(id=1), '
                       'cp_boot_id TEXT NOT NULL, owner_sha256 TEXT NOT NULL, domain TEXT NOT NULL, '
                       'first_id INTEGER NOT NULL, last_id INTEGER NOT NULL, proved_at REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS flow_id_generation_archive (cp_boot_id TEXT, owner_sha256 TEXT, '
                       'domain TEXT, first_id INTEGER, last_id INTEGER, proved_at REAL, next_id INTEGER, retired_at REAL)')
            # The allocator's own table, in its schema, so status can read it before commissioning.
            db.execute('CREATE TABLE IF NOT EXISTS hardware_flow_ids '
                       '(id INTEGER PRIMARY KEY CHECK(id=1), first_id INTEGER NOT NULL, '
                       'last_id INTEGER NOT NULL, domain TEXT NOT NULL, next_id INTEGER NOT NULL)')

    def subprocess_probe(self):
        argv = [sys.executable, os.path.abspath(__file__), 'probe', '--first', str(self.first), '--last', str(self.last)]
        result = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
        if result.returncode:
            raise RuntimeError('range probe failed: ' + (result.stderr or result.stdout)[-300:])
        return json.loads(result.stdout)

    def generation(self):
        row = self.db.execute('SELECT cp_boot_id,owner_sha256,domain,first_id,last_id,proved_at FROM flow_id_generation WHERE id=1').fetchone()
        return None if row is None else dict(zip(('cp_boot_id', 'owner_sha256', 'domain', 'first', 'last', 'proved_at'), row))

    def rollover(self, previous):
        """A new boot cleared the tables: archive the previous generation and its counter."""
        counter = self.db.execute('SELECT next_id FROM hardware_flow_ids WHERE id=1').fetchone()
        with self.db:
            self.db.execute('INSERT INTO flow_id_generation_archive VALUES (?,?,?,?,?,?,?,?)',
                            (previous['cp_boot_id'], previous['owner_sha256'], previous['domain'], previous['first'],
                             previous['last'], previous['proved_at'], counter[0] if counter else None, time.time()))
            self.db.execute('DELETE FROM hardware_flow_ids')
            self.db.execute('DELETE FROM flow_id_generation')
        self.log('flow-ID namespace of boot %s archived' % previous['cp_boot_id'][:8])

    def ensure(self, current):
        """Return the allocator for this boot, commissioning it when the hardware allows; None otherwise."""
        from ffn_fe100_flow_ids import FlowIds
        if current is not None:
            return current
        generation = self.generation()
        if generation is not None and generation['cp_boot_id'] == self.boot:
            expected = domain_digest(self.boot, generation['owner_sha256'], self.first, self.last)
            if generation['domain'] != expected or (generation['first'], generation['last']) != (self.first, self.last):
                self.reason = 'flow-ID generation journal does not match this owner; recovery review required'
                return None
            try:
                allocator = FlowIds(self.db, self.first, self.last, generation['domain'])
            except RuntimeError as error:
                self.reason = 'flow-ID journal: ' + str(error)[:200]
                return None
            self.reason = None
            return allocator
        now = self.clock()
        if self.attempted is not None and now - self.attempted < RETRY_SECONDS:
            return None
        self.attempted = now
        try:
            report = self.prober()
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            self.reason = 'range probe unavailable: ' + str(error)[:200]
            return None
        self.last_probe = report
        if report.get('cp_boot_id') != self.boot:
            self.reason = 'readiness reports another boot'
            return None
        if not report.get('initialized'):
            self.reason = 'FE100 not initialised in this boot: ' + '; '.join(report.get('blockers', []))[:200]
            return None
        if not report.get('proved') or not report.get('cleanup_verified'):
            self.reason = 'range proof failed: ' + str(report.get('error') or report.get('cleanup_error') or report.get('blockers'))[:200]
            return None
        if generation is not None:
            self.rollover(generation)
        domain = domain_digest(self.boot, report['owner_sha256'], self.first, self.last)
        with self.db:
            self.db.execute('INSERT INTO flow_id_generation VALUES (1,?,?,?,?,?,?)',
                            (self.boot, report['owner_sha256'], domain, self.first, self.last, time.time()))
        allocator = FlowIds(self.db, self.first, self.last, domain, initialize=True)
        self.reason = None
        self.log('flow-ID namespace commissioned for boot %s: %d..%d' % (self.boot[:8], self.first, self.last))
        return allocator

    def status(self, allocator):
        generation = self.generation()
        counter = self.db.execute('SELECT next_id FROM hardware_flow_ids WHERE id=1').fetchone()
        return dict(commissioned=allocator is not None, first=self.first, last=self.last, reason=self.reason,
                    cp_boot_id=generation['cp_boot_id'] if generation else None,
                    domain=generation['domain'][:12] if generation else None,
                    next_id=counter[0] if counter else None,
                    remaining_pairs=((self.last - counter[0] + 1) // 2) if counter else None)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['probe'])
    parser.add_argument('--first', type=int, default=FIRST)
    parser.add_argument('--last', type=int, default=LAST)
    args = parser.parse_args(argv)
    sys.path.insert(0, '/usr/local/sbin')
    print(json.dumps(probe(args.first, args.last)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
