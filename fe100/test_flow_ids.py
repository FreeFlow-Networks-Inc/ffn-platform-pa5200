import sqlite3
import tempfile
import unittest
from pathlib import Path
from ffn_fe100_flow_ids import FlowIds,assign_pair
from ffn_fe100_nat import session_pair4
from test_nat_sessions import row,reverse


class FixtureIds:
    """In-memory allocation only for policy unit tests with fake hardware."""
    def __init__(self):self.next_id=100000
    def reserve_pair(self):
        result=self.next_id,self.next_id+1;self.next_id+=2;return result


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'ids.sqlite3'
        self.db=sqlite3.connect(self.path);self.addCleanup(self.db.close)

    def test_restart_and_failed_install_never_reuse_ids(self):
        owner=FlowIds(self.db,1000,1999,'a'*64,initialize=True)
        self.assertEqual(owner.reserve_pair(),(1000,1001))
        # Simulate termination after reservation but before hardware install.
        self.db.close();self.db=sqlite3.connect(self.path);self.addCleanup(self.db.close)
        owner=FlowIds(self.db,1000,1999,'a'*64)
        self.assertEqual(owner.reserve_pair(),(1002,1003))

    def test_no_automatic_commissioning_or_namespace_reset(self):
        with self.assertRaisesRegex(RuntimeError,'not been commissioned'):
            FlowIds(self.db,1,99,'a'*64)
        owner=FlowIds(self.db,1,99,'a'*64,initialize=True);owner.reserve_pair()
        for first,last,domain in ((2,99,'a'*64),(1,199,'a'*64),(1,99,'b'*64)):
            with self.assertRaisesRegex(RuntimeError,'namespace changed'):
                FlowIds(self.db,first,last,domain,initialize=True)
        self.assertEqual(FlowIds(self.db,1,99,'a'*64,initialize=True).reserve_pair(),(3,4))

    def test_exhaustion_never_wraps_and_fences_allocator(self):
        owner=FlowIds(self.db,0xfffffffe,0xffffffff,'a'*64,initialize=True)
        self.assertEqual(owner.reserve_pair(),(0xfffffffe,0xffffffff))
        with self.assertRaisesRegex(RuntimeError,'exhausted'):owner.reserve_pair()
        with self.assertRaisesRegex(RuntimeError,'fenced'):owner.reserve_pair()
        with self.assertRaisesRegex(RuntimeError,'exhausted'):
            FlowIds(self.db,0xfffffffe,0xffffffff,'a'*64).reserve_pair()

    def test_nested_transaction_never_returns_uncommitted_ids(self):
        owner=FlowIds(self.db,1,99,'a'*64,initialize=True)
        self.db.execute('BEGIN')
        with self.assertRaisesRegex(RuntimeError,'uncommitted'):owner.reserve_pair()
        self.db.rollback();self.assertEqual(owner.reserve_pair(),(1,2))

    def test_journal_error_is_fenced_before_hardware(self):
        owner=FlowIds(self.db,1,99,'a'*64,initialize=True)
        self.db.execute('PRAGMA query_only=ON')
        with self.assertRaises(sqlite3.OperationalError):owner.reserve_pair()
        self.db.execute('PRAGMA query_only=OFF')
        with self.assertRaisesRegex(RuntimeError,'fenced'):owner.reserve_pair()
        self.assertEqual(FlowIds(self.db,1,99,'a'*64).reserve_pair(),(1,2))

    def test_assignment_changes_only_ids_and_requires_allocator(self):
        entries=session_pair4(7,row(),reverse(row('203.0.113.9',sport=45000)),[4,5],[30,31])
        with self.assertRaisesRegex(RuntimeError,'not commissioned'):assign_pair(entries,None)
        owner=FlowIds(self.db,100,199,'a'*64,initialize=True)
        changed=assign_pair(entries,owner)
        self.assertEqual([int.from_bytes(e[36:40],'big') for e in changed],[100,101])
        for a,b in zip(entries,changed):self.assertEqual(a[:36]+a[40:],b[:36]+b[40:])

    def test_receiver_restart_ignores_late_prior_session_statistics(self):
        from ffn_fe100_flowstats import NativeCounterStream,flow_id_of
        entries=session_pair4(7,row(),reverse(row()),[4,5],[30,31])
        old=assign_pair(entries,FlowIds(self.db,100,199,'a'*64,initialize=True))
        new=assign_pair(entries,FlowIds(self.db,100,199,'a'*64))
        stream=NativeCounterStream('same-hardware-generation')
        for entry in new:stream.register(entry)
        stream.consume('same-hardware-generation',dict(sequence=1,elapsed_ms=1,
            records=[dict(flow_id=flow_id_of(e),packets=5,octets=320,reason=1) for e in old]))
        self.assertEqual(stream.ignored,2)
        self.assertTrue(all(stream.totals(e)==dict(packets=0,octets=0) for e in new))

    def test_invalid_range_and_unsafe_durability(self):
        for first,last in ((0,99),(True,99),(1,1),(1,1<<32)):
            with self.assertRaises(ValueError):FlowIds(self.db,first,last,'a'*64,initialize=True)
        self.db.execute('PRAGMA synchronous=NORMAL')
        with self.assertRaisesRegex(RuntimeError,'durability'):FlowIds(self.db,1,99,'a'*64,initialize=True)


if __name__=='__main__':unittest.main()
