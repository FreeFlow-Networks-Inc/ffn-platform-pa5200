import json
from pathlib import Path
import tempfile
import unittest
from ffn_fe100_guard import publish
from ffn_fe100_lab_guard import Recovery


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'physical.json';self.events=[]
        self.record=dict(schema=1,cleanup_owner='guardian',bcm_epoch='epoch',lab_journal='exact',
                         redirect_touched=True,rules=[dict(group=4,entry=7)],rule_pending=False)
        publish(self.path,self.record)
        class Lab:
            record={}
            def restore(inner):self.events.append('flows-and-resources')
            def close(inner):self.events.append('closed')
        self.lab=Lab()
        def factory(recovery):
            self.assertEqual(recovery,'exact');self.events.append('generation-check');return self.lab
        self.recover=Recovery(self.path,epoch=lambda:'epoch',lab_factory=factory,route=self.route)

    def route(self,mode,ids=None):
        self.events.append(mode)
        if mode=='offload-rule-delete':
            self.assertEqual(json.loads(self.path.read_text())['delete_pending'],0)
        return dict(completed=True)

    def test_order_and_idempotent_acknowledged_recovery(self):
        self.assertTrue(self.recover('owner exited'))
        self.assertEqual(self.events,['generation-check','front5-session-restore','flows-and-resources',
                         'offload-rule-delete','session-group-absent','closed'])
        self.events=[];self.assertTrue(self.recover('startup recovery'))
        self.assertFalse(self.events)

    def test_missing_ingress_ack_stops_before_flow_resources(self):
        self.recover.route=lambda *args:dict(completed=False)
        with self.assertRaisesRegex(RuntimeError,'not acknowledged'):self.recover('owner exited')
        self.assertEqual(self.events,['generation-check','closed'])

    def test_both_ingresses_withdraw_before_shared_resources(self):
        self.record['redirects']=['front5-session-restore','session-path-restore']
        publish(self.path,self.record)
        self.assertTrue(self.recover('owner exited'))
        self.assertEqual(self.events[:4],['generation-check','session-path-restore',
                                         'front5-session-restore','flows-and-resources'])

    def test_partial_ingress_withdrawal_preserves_shared_resources(self):
        self.record['redirects']=['front5-session-restore','session-path-restore']
        publish(self.path,self.record)
        def route(mode,ids=None):
            self.events.append(mode)
            return dict(completed=mode!='front5-session-restore')
        self.recover.route=route
        with self.assertRaisesRegex(RuntimeError,'not acknowledged'):self.recover('owner exited')
        self.assertEqual(self.events,['generation-check','session-path-restore','front5-session-restore','closed'])
        self.assertNotIn('flows-and-resources',self.events)

    def test_epoch_change_stops_all_hardware_access(self):
        self.record['bcm_epoch']='old';publish(self.path,self.record)
        with self.assertRaisesRegex(RuntimeError,'lifetime'):self.recover('owner exited')
        self.assertFalse(self.events)

    def test_uncertain_sdk_operations_never_adopt_ids(self):
        for field,value in [('rule_pending',True),('delete_pending',0)]:
            with self.subTest(field=field):
                publish(self.path,dict(self.record,**{field:value}));self.events=[]
                with self.assertRaisesRegex(RuntimeError,'uncertain'):self.recover('owner exited')
                self.assertIn('flows-and-resources',self.events)
                self.assertNotIn('offload-rule-delete',self.events)
                self.assertFalse(json.loads(self.path.read_text())['withdrawal_acknowledged'])

    def test_flow_failure_keeps_bcm_resources(self):
        def fail():raise RuntimeError('flow deletion unacknowledged')
        self.lab.restore=fail
        with self.assertRaisesRegex(RuntimeError,'flow deletion'):self.recover('owner exited')
        self.assertNotIn('offload-rule-delete',self.events)
        self.assertEqual(self.events[-1],'closed')

    def test_lost_sdk_delete_response_is_never_retried(self):
        def lost(mode,ids=None):
            self.route(mode,ids)
            if mode=='offload-rule-delete':raise OSError('lost response')
            return dict(completed=True)
        self.recover.route=lost
        with self.assertRaises(OSError):self.recover('owner exited')
        self.events=[]
        with self.assertRaisesRegex(RuntimeError,'uncertain'):self.recover('startup recovery')
        self.assertNotIn('offload-rule-delete',self.events)

    def test_missing_journal_only_valid_before_first_launch(self):
        self.path.unlink();self.assertTrue(self.recover('startup recovery'))
        with self.assertRaisesRegex(RuntimeError,'missing'):self.recover('owner exited')


if __name__=='__main__':unittest.main()
