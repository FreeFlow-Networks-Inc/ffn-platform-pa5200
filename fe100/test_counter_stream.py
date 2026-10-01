#!/usr/bin/env python3
import unittest
from ffn_fe100_flowstats import NativeCounterStream
from ffn_fe100_sessions import forwarding_entry4,key4

class CounterStream(unittest.TestCase):
    def setUp(self):
        self.now=100
        self.stream=NativeCounterStream('test-table-epoch',clock=lambda:self.now)
        self.entry=forwarding_entry4(key4('198.18.0.1','198.18.0.2',49000,49001,17,4094),1001,31)
        self.stream.register(self.entry)

    def event(self, sequence, **changes):
        record=dict(flow_id=1001,packets=64,octets=8256,reason=1)
        record.update(changes)
        return dict(sequence=sequence,elapsed_ms=sequence*100,records=[record])

    def consume(self, sequence, **changes):
        self.stream.consume('test-table-epoch',self.event(sequence,**changes))

    def test_equal_deltas_are_distinct_activity(self):
        self.assertIsNone(self.stream(self.entry))
        self.consume(1);first=self.stream(self.entry)
        self.consume(2)
        self.assertNotEqual(first,self.stream(self.entry))
        self.assertEqual(self.stream.totals(self.entry),dict(packets=128,octets=16512))

    def test_lost_or_replayed_events_latch_unavailable(self):
        self.consume(1)
        for sequence in (1,3):
            with self.assertRaises((ValueError,RuntimeError)):self.consume(sequence)
        self.assertFalse(self.stream.available)
        self.assertIsNone(self.stream(self.entry))
        self.assertEqual(self.stream.totals(self.entry)['packets'],64)

    def test_foreign_epoch_invalidates_without_accounting(self):
        with self.assertRaises(ValueError):self.stream.consume('other',self.event(1))
        self.assertEqual(self.stream.totals(self.entry)['packets'],0)
        self.assertFalse(self.stream.available)

    def test_complete_validation_precedes_accounting(self):
        event=self.event(1);event['records'].append(dict(flow_id=1001,packets=256,octets=1,reason=1))
        with self.assertRaises(ValueError):self.stream.consume('test-table-epoch',event)
        self.assertEqual(self.stream.totals(self.entry)['packets'],0)

    def test_retired_id_cannot_receive_late_report_or_be_reused(self):
        self.stream.forget(1001);self.consume(1)
        self.assertEqual(self.stream.ignored,1)
        self.assertIsNone(self.stream.totals(self.entry))
        with self.assertRaises(ValueError):self.stream.register(self.entry)

    def test_unowned_records_do_not_allocate_memory(self):
        self.consume(1,flow_id=99)
        self.assertEqual(len(self.stream.flows),1)
        self.assertIsNone(self.stream(self.entry))

    def test_reason_two_counts_without_refreshing_activity(self):
        self.consume(1,reason=2)
        self.assertEqual(self.stream.totals(self.entry)['packets'],64)
        self.assertIsNone(self.stream(self.entry))
        self.consume(2);token=self.stream(self.entry)
        self.now+=4;self.consume(3,reason=2)
        self.assertEqual(self.stream(self.entry),token)
        self.now+=2;self.assertIsNone(self.stream(self.entry))

    def test_expired_or_failed_receiver_does_not_look_idle(self):
        self.consume(1);self.now+=6
        self.assertIsNone(self.stream(self.entry))
        self.stream.receiver_finished(dict(capture_drops=1))
        self.assertFalse(self.stream.available)

    def test_clean_exit_also_ends_lease(self):
        self.consume(1)
        self.stream.receiver_finished(dict(failed=False,malformed=0,capture_drops=0))
        self.assertIsNone(self.stream(self.entry))

    def test_clock_regression_invalidates_activity(self):
        self.consume(1);self.now=99
        self.assertIsNone(self.stream(self.entry))
        self.assertFalse(self.stream.available)

    def test_nonfinite_clock_cannot_make_activity_permanent(self):
        self.consume(1);self.now=float('nan')
        self.assertIsNone(self.stream(self.entry))
        self.assertFalse(self.stream.available)

    def test_id_budget_includes_retired_ids(self):
        stream=NativeCounterStream('epoch',max_ids=1);stream.register(self.entry);stream.forget(1001)
        entry=bytearray(self.entry);entry[36:40]=(1002).to_bytes(4,'big')
        with self.assertRaises(RuntimeError):stream.register(entry)

    def test_boolean_and_negative_values_are_not_counters(self):
        for field,value in [('sequence',True),('elapsed_ms',-1),('packets',True),('reason',4)]:
            with self.subTest(field=field):
                stream=NativeCounterStream('epoch');event=self.event(1)
                if field in event:event[field]=value
                else:event['records'][0][field]=value
                with self.assertRaises(ValueError):stream.consume('epoch',event)
                self.assertFalse(stream.available)

if __name__=='__main__':unittest.main()
