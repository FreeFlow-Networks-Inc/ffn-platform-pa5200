import sqlite3
import tempfile
import unittest
from pathlib import Path
import ffn_fe100_flow_namespace as ns

BOOT = 'aaaaaaaa-1111-4111-8111-111111111111'
OTHER = 'bbbbbbbb-2222-4222-8222-222222222222'
OWNER = 'c' * 64


def report(**changes):
    value = dict(schema=1, cp_boot_id=BOOT, owner_sha256=OWNER, initialized=True, blockers=[], first=ns.FIRST, last=ns.LAST,
                 proved=True, cleanup_verified=True, operations=[])
    value.update(changes)
    return value


class FlowNamespaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'journal.sqlite3')
        self.db = self.connect(); self.now = 1000.0; self.probes = []; self.logs = []

    def connect(self):
        db = sqlite3.connect(self.path); db.execute('PRAGMA synchronous=FULL'); self.addCleanup(db.close); return db

    def namespace(self, db=None, boot=BOOT, prober=None):
        return ns.FlowNamespace(db or self.db, boot, clock=lambda: self.now,
                                prober=prober or (lambda: self.probes.append(1) or report()), log=self.logs.append)

    def test_commissions_on_an_initialised_hardware_after_the_range_proof(self):
        space = self.namespace()
        allocator = space.ensure(None)
        self.assertIsNotNone(allocator); self.assertEqual(self.probes, [1]); self.assertIsNone(space.reason)
        status = space.status(allocator)
        self.assertEqual((status['commissioned'], status['first'], status['last'], status['cp_boot_id'], status['next_id']),
                         (True, ns.FIRST, ns.LAST, BOOT, ns.FIRST))
        self.assertEqual(status['domain'], ns.domain_digest(BOOT, OWNER, ns.FIRST, ns.LAST)[:12])
        self.assertEqual(allocator.reserve_pair(), (ns.FIRST, ns.FIRST + 1))
        self.assertIs(space.ensure(allocator), allocator); self.assertEqual(self.probes, [1])

    def test_same_boot_resumes_the_journal_without_reusing_ids(self):
        space = self.namespace(); allocator = space.ensure(None); allocator.reserve_pair(); allocator.reserve_pair()
        db = self.connect(); again = self.namespace(db)
        resumed = again.ensure(None)
        self.assertEqual(self.probes, [1])   # no second hardware proof in the same generation
        self.assertEqual(resumed.reserve_pair(), (ns.FIRST + 4, ns.FIRST + 5))
        self.assertEqual(again.status(resumed)['next_id'], ns.FIRST + 6)

    def test_new_boot_rolls_the_namespace_over_and_archives_the_old_one(self):
        space = self.namespace(); allocator = space.ensure(None); allocator.reserve_pair()
        db = self.connect(); later = self.namespace(db, boot=OTHER, prober=lambda: report(cp_boot_id=OTHER))
        fresh = later.ensure(None)
        self.assertEqual(fresh.reserve_pair(), (ns.FIRST, ns.FIRST + 1))
        archive = db.execute('SELECT cp_boot_id, next_id FROM flow_id_generation_archive').fetchall()
        self.assertEqual(archive, [(BOOT, ns.FIRST + 2)])
        self.assertEqual(later.status(fresh)['cp_boot_id'], OTHER); self.assertIn('archived', self.logs[-2])

    def test_not_initialised_or_failed_proof_commissions_nothing_and_retries_later(self):
        cases = [report(initialized=False, blockers=['FLU lacks verified initialization in this boot']),
                 report(proved=False, error='probe insert failed: 7'),
                 report(cleanup_verified=False, cleanup_error='delete failed'),
                 report(cp_boot_id=OTHER),
                 report(blockers=['hardware flow tables are not empty; the range proof needs empty tables'], initialized=False)]
        for value in cases:
            with self.subTest(reason=value.get('error') or value['blockers']):
                calls = []
                space = self.namespace(prober=lambda value=value: calls.append(1) or value)
                self.assertIsNone(space.ensure(None)); self.assertEqual(len(calls), 1); self.assertIsNotNone(space.reason)
                self.assertIsNone(space.ensure(None)); self.assertEqual(len(calls), 1)   # within the retry interval
                self.now += ns.RETRY_SECONDS
                self.assertIsNone(space.ensure(None)); self.assertEqual(len(calls), 2)
                self.assertFalse(space.status(None)['commissioned'])
                self.assertIsNone(self.db.execute('SELECT 1 FROM flow_id_generation').fetchone())
                self.now += ns.RETRY_SECONDS

    def test_probe_errors_are_reported_not_raised(self):
        def broken():
            raise RuntimeError('range probe failed: no FE100 BAR')
        space = self.namespace(prober=broken)
        self.assertIsNone(space.ensure(None)); self.assertIn('no FE100 BAR', space.reason)

    def test_journal_that_does_not_match_this_owner_is_fenced(self):
        space = self.namespace(); space.ensure(None)
        with self.db:
            self.db.execute("UPDATE flow_id_generation SET domain='0'*64")
        again = self.namespace(self.connect())
        self.assertIsNone(again.ensure(None)); self.assertIn('recovery review', again.reason)
        self.assertEqual(self.probes, [1])

    def test_domain_binds_boot_owner_and_range(self):
        base = ns.domain_digest(BOOT, OWNER, ns.FIRST, ns.LAST)
        self.assertEqual(len(base), 64)
        self.assertNotEqual(base, ns.domain_digest(OTHER, OWNER, ns.FIRST, ns.LAST))
        self.assertNotEqual(base, ns.domain_digest(BOOT, 'd' * 64, ns.FIRST, ns.LAST))
        self.assertNotEqual(base, ns.domain_digest(BOOT, OWNER, ns.FIRST, ns.LAST - 1))


if __name__ == '__main__':
    unittest.main()
