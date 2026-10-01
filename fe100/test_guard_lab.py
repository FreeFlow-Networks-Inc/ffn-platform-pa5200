import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import ffn_fe100_guard_lab as controller
import ffn_fe100_packet_lab as packet
import ffn_fe100_bcm_lab as bcm


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'guarded-physical-test.json'
        self.events=[]
        class Lab:
            path='exact-lab-journal'
            def call(inner,*args,**kwargs):
                return dict(commissioning_blockers=[],registers={'0x40428':0,'0x40450':0})
            def command(inner,op):self.events.append(op);return dict(ok=True)
            def close(inner):self.events.append('closed')
        self.lab=Lab()

    def route(self,request):
        record=json.loads(self.path.read_text())
        if request['mode'] in ('dsa-front5-create','dsa-front13-create'):
            self.assertTrue(record['rule_pending'])
        else:
            self.assertTrue(record['redirect_touched'])
            self.assertFalse(record['rule_pending'])
        self.events.append(request['mode'])
        return dict(completed=True,markers=['FFN_HW_RULE group=2 entry=8 stat=-1 rv=0',
                                           'FFN_HW_DATA dq1=3 dq2=4'])

    def run_commands(self,ops):
        with patch.object(controller,'Lease'),patch.object(packet,'Lab',return_value=self.lab), \
             patch.dict(sys.modules,ffn_copper_forwarding=SimpleNamespace(epoch=lambda:'epoch')), \
             patch.object(bcm,'run',side_effect=self.route), \
             patch.object(controller.select,'select',return_value=([1],[],[])), \
             patch.object(controller.sys,'stdin',io.StringIO(''.join(json.dumps(dict(op=op))+'\n' for op in ops))), \
             patch.object(controller.sys,'stdout',io.StringIO()):
            controller.serve(self.path)

    def test_intent_precedes_sdk_mutation_and_parent_owns_cleanup(self):
        self.run_commands(['prepare','install','finish'])
        self.assertEqual(self.events,['prepare','dsa-front5-create','front5-session-enable',
                                     'snapshot','install','closed'])
        record=json.loads(self.path.read_text())
        self.assertEqual(record['cleanup_owner'],'guardian')
        self.assertNotEqual(record['stage'],'restored')

    def test_duplicate_prepare_never_allocates_twice(self):
        with self.assertRaisesRegex(RuntimeError,'already prepared'):self.run_commands(['prepare','prepare'])
        self.assertEqual(self.events.count('dsa-front5-create'),1)

    def test_split_path_records_both_ingresses_before_activation(self):
        with patch.object(packet,'SPLIT_PATH_LAB',True):
            self.run_commands(['prepare','install','finish'])
        self.assertEqual(self.events,['prepare','dsa-front5-create','dsa-front13-create',
                                     'front5-session-enable','session-path-enable','snapshot','install','closed'])
        record=json.loads(self.path.read_text())
        self.assertEqual(record['redirects'],['front5-session-restore','session-path-restore'])
        self.assertEqual(len(record['rules']),2)

    def test_fault_injection_is_opt_in(self):
        for op in ('guard-crash','guard-stall'):
            with self.subTest(op=op),self.assertRaisesRegex(ValueError,'unsupported'):
                self.run_commands([op])


if __name__=='__main__':unittest.main()
