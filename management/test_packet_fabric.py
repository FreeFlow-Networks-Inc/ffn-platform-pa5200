import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import ffn_packet_fabric as fabric


class FabricTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'state.json'
        self.patches=[patch.object(fabric,'STATE',self.path),patch.object(fabric,'LOCK',Path(self.temp.name)/'fabric.lock'),patch('ffn_aggregate_hardware.epoch',return_value='epoch')]
        for p in self.patches:p.start()
        self.queues={24:0,34:0,35:0};self.header=1;self.writes=[];self.fail=False

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def read(self,port,operation=0):
        if operation:
            self.writes.append((port,operation))
            self.assertIn('pending',json.loads(self.path.read_text()))
            if self.fail:raise RuntimeError('lost SDK response')
            if operation==2:self.header=11
            else:
                self.assertEqual(self.queues[port],0);self.queues[port]=8
        return dict(port=port,queues=self.queues[port],header=self.header,bundles=sum(v//8 for v in self.queues.values()),allocations=[])

    def test_missing_steps_only_and_repeated_reconcile_is_read_only(self):
        self.assertTrue(fabric.ensure([23,24],'epoch',self.read)['ready'])
        self.assertEqual(self.writes,[(24,2),(24,1),(34,1),(35,1)])
        self.writes=[]
        self.assertTrue(fabric.ensure([24,23],'epoch',self.read)['ready'])
        self.assertEqual(self.writes,[])
        self.assertNotIn('pending',json.loads(self.path.read_text()))

    def test_unknown_partial_allocation_is_never_repeated(self):
        self.header=11;self.fail=True
        with self.assertRaisesRegex(RuntimeError,'lost SDK'):fabric.ensure([23,24],'epoch',self.read)
        self.assertEqual(self.writes,[(24,1)])
        self.fail=False
        with self.assertRaisesRegex(RuntimeError,'automatic retry withheld'):fabric.ensure([23,24],'epoch',self.read)
        self.assertEqual(self.writes,[(24,1)])

    def test_changed_epoch_discards_old_journal_but_checks_live_resources(self):
        self.path.write_text(json.dumps(dict(epoch='old',pending=dict(port=34))))
        self.header=11;self.queues[24]=8
        fabric.ensure([23,24],'epoch',self.read)
        self.assertEqual(self.writes,[(34,1),(35,1)])
        self.assertEqual(json.loads(self.path.read_text())['epoch'],'epoch')

    def test_conflict_active_header_and_stale_epoch_do_not_write(self):
        for case in ('epoch','queues','header'):
            with self.subTest(case=case):
                self.queues={24:0,34:0,35:0};self.header=1
                if case=='queues':self.queues[35]=4
                if case=='header':self.queues[35]=8
                with self.assertRaises(RuntimeError):fabric.ensure([23,24],'wrong' if case=='epoch' else 'epoch',self.read)
                self.assertEqual(self.writes,[])

    def test_failed_readback_keeps_pending_for_recovery(self):
        self.header=11
        def read(port,operation=0):
            if operation:return dict(queues=0,header=11)
            return self.read(port)
        with self.assertRaisesRegex(RuntimeError,'not verified'):fabric.ensure([23,24],'epoch',read)
        self.assertEqual(json.loads(self.path.read_text())['pending'],dict(operation='queues',port=24))

    def test_wan_recovery_preserves_active_aggregate_and_trunk_queues(self):
        self.header=11;self.queues={24:8,34:8,35:8,28:0}
        self.assertTrue(fabric.ensure([1],'epoch',self.read)['ready'])
        self.assertEqual(self.writes,[(28,1)])
        self.assertEqual(self.queues,{24:8,34:8,35:8,28:8})
        self.writes=[]
        fabric.ensure([1],'epoch',self.read)
        self.assertEqual(self.writes,[])


if __name__=='__main__':unittest.main()
