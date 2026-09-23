import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'management'))
import ffn_fe100_bcm_lab as lab


class BcmLabTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.script=self.root/'script.c';self.script.write_text('production recipe')
        self.state=self.root/'baseline.json'
        self.patches=[patch.object(lab,'ROOT',self.root),patch.object(lab,'STATE',self.state),
                      patch.object(lab,'SCRIPT',self.script),patch.object(lab,'FORWARD_LOCK',self.root/'forward.lock')]
        for p in self.patches:p.start()
        self.routes={16:(0,0),7:(24,1)};self.writes=[];self.fail=None

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def call(self,request):
        source=self.script.read_text()
        setter=re.search(r'bcm_port_force_forward_set\(0,(\d+),(\d+),(\d+)\)',source)
        if setter:
            port,dst,enabled=map(int,setter.groups());self.writes.append(port)
            self.assertTrue(self.state.exists())
            self.routes[port]=(dst,enabled)
            if port==self.fail:raise OSError('lost reply')
        port=int(re.search(r'bcm_port_force_forward_get\(0,(\d+),',source)[1])
        dst,enabled=self.routes[port]
        return dict(completed=True,markers=[f'FFN_LAB_ROUTE port={port} destination={dst} enabled={enabled} rv=0','FFN_DONE'])

    def test_script_is_restored_on_success_and_transport_failure(self):
        lab.execute(lab.port_recipe(7),self.call)
        self.assertEqual(self.script.read_text(),'production recipe')
        with self.assertRaises(OSError):lab.execute('lab',lambda _:(_ for _ in ()).throw(OSError()))
        self.assertEqual(self.script.read_text(),'production recipe')

    def test_lab_yields_with_shared_lock_released_for_production(self):
        import fcntl
        waits=[]
        def pause(seconds):
            with lab.FORWARD_LOCK.open('a') as production:
                fcntl.flock(production,fcntl.LOCK_EX|fcntl.LOCK_NB)
                waits.append(seconds)
        with patch.object(lab.time,'sleep',side_effect=pause):
            lab.execute(lab.port_recipe(7),self.call)
        self.assertEqual(waits,[.1])

    def test_baseline_restores_original_enabled_state_after_partial_failure(self):
        # save()'s default path is fixed at definition time, inject the fixture.
        original_save=lab.save
        with patch.object(lab,'save',side_effect=lambda record:original_save(record,self.state)):
            self.fail=7
            with self.assertRaises(OSError):lab.baseline('baseline-begin',self.call,'epoch')
            self.assertEqual(json.loads(self.state.read_text())['stage'],'preparing')
            self.fail=None;lab.baseline('baseline-end',self.call,'epoch')
            self.assertEqual(self.routes[16][1],0);self.assertEqual(self.routes[7],(24,1))
            self.assertEqual(json.loads(self.state.read_text())['stage'],'restored')

    def test_pending_baseline_and_changed_owner_or_boot_are_not_adopted(self):
        self.state.write_text(json.dumps(dict(epoch='epoch',stage='active',ports=[dict(port=16,destination=0,enabled=0)])))
        for mode,epoch in (('baseline-begin','epoch'),('baseline-end','other')):
            with self.assertRaises(RuntimeError):lab.baseline(mode,self.call,epoch)
        self.routes[16]=(3,1)
        with self.assertRaises(RuntimeError):lab.baseline('baseline-end',self.call,'epoch')
        self.assertEqual(self.writes,[])

    def test_no_broad_allocation_link_control_or_unknown_cleanup_ids(self):
        for mode in ('dp-queues-allocate','front-allocate','autoneg-enable','nif-enable'):
            with self.assertRaises(ValueError):lab.render(mode,{})
        with self.assertRaises(ValueError):lab.render('offload-rule-delete',{})
        with self.assertRaises(ValueError):lab.port_recipe(34,24,1)

    def test_queue_preparation_preserves_production_and_refuses_uncertain_retry(self):
        queues={24:8,3:0,7:0,8:8,16:0};writes=[]
        def execute(source,call):
            port,op=map(int,re.search(r'int p=(\d+);int op=(\d+);',source).groups())
            if op:
                record=json.loads((self.root/'bcm-lab-queues.json').read_text())
                self.assertEqual(record['pending'],port)
                writes.append(port);queues[port]=8
            return dict(completed=True,markers=[f'FFN_FABRIC_STATE port={port} queues={queues[port]} bundles=4 header=11 rv=0','FFN_FABRIC_DONE'])
        with patch.object(lab,'execute',side_effect=execute):
            lab.prepare_queues(None,'epoch');self.assertEqual(writes,[3,7,16])
            lab.prepare_queues(None,'epoch');self.assertEqual(writes,[3,7,16])
            path=self.root/'bcm-lab-queues.json';record=json.loads(path.read_text());record['pending']=7;path.write_text(json.dumps(record))
            with self.assertRaises(RuntimeError):lab.prepare_queues(None,'epoch')


if __name__=='__main__':unittest.main()
