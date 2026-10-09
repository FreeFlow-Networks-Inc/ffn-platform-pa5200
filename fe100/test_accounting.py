import unittest
from ffn_fe100_accounting import AccountedSession
from ffn_fe100_flowstats import NativeCounterStream
from ffn_fe100_sessions import key4,nat_entry4


class Lease:
    original=('192.0.2.10','198.51.100.20',42001,443)
    reply=('198.51.100.20','203.0.113.30',443,52001)
    protocol=17
    def __init__(self):self.updates=[];self.closed=False;self.fail=False
    def update(self,*args):
        if self.fail:raise OSError('stale connection')
        self.updates.append(args)
    def close(self):self.closed=True
    def check(self):
        if self.fail:raise OSError('stale idle connection')


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.time=10
        self.stream=NativeCounterStream('owner/table',clock=lambda:self.time,receiver_timeout=2)
        self.heartbeat()
        self.entries=[nat_entry4(key4(*value,17,4094),ident,31,dict(zip(
            ('source','destination','source_port','destination_port'),out))) for value,out,ident in
            [(Lease.original,('203.0.113.30','198.51.100.20',52001,443),1001),
             (Lease.reply,('198.51.100.20','192.0.2.10',443,42001),1003)]]
        for entry in self.entries:self.stream.register(entry)
        self.lease=Lease();self.withdrawals=0;self.ack=True
        self.owner=AccountedSession(self.stream,self.entries,self.lease,self.withdraw)

    def withdraw(self):
        self.withdrawals+=1
        self.assertFalse(self.lease.closed)
        return self.ack

    def report(self,reason=1):
        self.stream.consume('owner/table',{'sequence':self.stream.sequence+1,'elapsed_ms':int((self.time-10)*1000),
            'records':[{'flow_id':i,'packets':32,'octets':4512,'reason':reason} for i in (1001,1003)]})

    def heartbeat(self):
        self.stream.consume('owner/table',{'sequence':self.stream.sequence+1,
            'elapsed_ms':int((self.time-10)*1000),'health':True})

    def test_equal_reports_each_count_once(self):
        for i in (1,2):
            self.report();self.assertTrue(self.owner.sync())
            self.assertEqual(self.lease.updates[-1],(i,(32,32),(4512,4512),3))
            self.assertFalse(self.owner.sync())
        self.assertEqual(len(self.lease.updates),2)

    def test_reason_two_counts_without_timeout_refresh(self):
        self.report(2);self.owner.sync()
        self.assertEqual(self.lease.updates[-1][-1],0)

    def test_receive_loss_withdraws_before_close(self):
        self.report();self.stream.receiver_finished({'capture_drops':1})
        with self.assertRaises(RuntimeError):self.owner.sync()
        self.assertTrue(self.lease.closed);self.assertEqual(self.withdrawals,1)
        self.assertEqual(self.lease.updates,[])

    def test_kernel_failure_never_retries_delta(self):
        self.report();self.lease.fail=True
        with self.assertRaises(OSError):self.owner.sync()
        self.lease.fail=False
        with self.assertRaises(RuntimeError):self.owner.sync()
        self.assertEqual(self.lease.updates,[])

    def test_failed_withdrawal_keeps_lease(self):
        self.ack=False;self.stream.invalidate('lost receiver')
        with self.assertRaises(RuntimeError):self.owner.sync()
        self.assertFalse(self.lease.closed)
        self.assertIn(1001,self.stream.flows)
        self.ack=True;self.owner.fence('retry withdrawal')
        self.assertTrue(self.lease.closed)
        self.assertIn(1001,self.stream.retired)

    def test_delayed_accounting_fences(self):
        self.report();self.time+=6
        with self.assertRaises(RuntimeError):self.owner.sync()
        self.assertEqual(self.lease.updates,[])

    def test_new_reason_two_does_not_make_old_activity_fresh(self):
        self.report()
        for _ in range(6):self.time+=1;self.heartbeat()
        self.report(2)
        with self.assertRaises(RuntimeError):self.owner.sync()
        self.assertEqual(self.lease.updates,[])

    def test_epoch_changed_fences(self):
        self.report();self.stream.epoch='replacement'
        with self.assertRaises(RuntimeError):self.owner.sync()
        self.assertTrue(self.lease.closed)

    def test_register_after_activity_rejected(self):
        self.report()
        with self.assertRaises(ValueError):AccountedSession(self.stream,self.entries,self.lease,self.withdraw)

    def test_unrelated_conntrack_rejected(self):
        self.lease.original=('192.0.2.11','198.51.100.20',42001,443)
        with self.assertRaises(ValueError):AccountedSession(self.stream,self.entries,self.lease,self.withdraw)

    def test_direction_reversal_rejected(self):
        with self.assertRaises(ValueError):AccountedSession(self.stream,self.entries[::-1],self.lease,self.withdraw)

    def test_retired_id_cannot_be_rebound(self):
        self.owner.fence('configuration changed')
        with self.assertRaises(ValueError):self.stream.register(self.entries[0])

    def test_idle_sync_does_not_refresh(self):
        self.time+=1;self.heartbeat()
        self.assertFalse(self.owner.sync())
        self.assertEqual(self.lease.updates,[])

    def test_idle_receiver_death_withdraws(self):
        self.time+=2
        with self.assertRaises(RuntimeError):self.owner.sync()
        self.assertTrue(self.lease.closed)
        self.assertEqual(self.withdrawals,1)

    def test_unsupervised_receiver_cannot_bind_accounting(self):
        self.stream.receiver_timeout=None
        with self.assertRaises(RuntimeError):AccountedSession(self.stream,self.entries,self.lease,self.withdraw)

    def test_idle_deleted_session_withdraws_without_refresh(self):
        self.lease.fail=True
        with self.assertRaises(OSError):self.owner.sync()
        self.assertTrue(self.lease.closed)
        self.assertEqual(self.withdrawals,1)
        self.assertEqual(self.lease.updates,[])


if __name__=='__main__':unittest.main()
