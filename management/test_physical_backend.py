import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import physical_backend as backend


class PhysicalBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'running';self.intent=Path(self.tmp.name)/'intent'
        self.path.write_text('<config><devices><entry><network><interface><ethernet><entry name="ethernet1/5"><link-state>down</link-state><layer3/></entry></ethernet></interface></network></entry></devices></config>')
        self.calls=[];self.fail=False
        for name,value in [('RUNNING',self.path),('INTENT',self.intent)]:
            p=patch.object(backend,name,value);p.start();self.addCleanup(p.stop)
        self.payload=dict(port=5,operation='attach',running_revision=hashlib.sha256(self.path.read_bytes()).hexdigest(),
                          revision=backend.revision(dict(epoch='epoch',ready=False,state={}),dict(boot_id='boot',running=False)))
    def call(self,role,action,port,payload):
        self.calls.append((role,action))
        if role=='cp':return dict(epoch='epoch',ready=action=='start',state={})
        if action=='start' and self.fail:raise RuntimeError('DP rejected attachment')
        return dict(boot_id='boot',running=action=='start')
    def test_down_port_validation_is_read_only(self):
        self.assertTrue(backend.execute('validate',self.payload,self.call)['validated'])
        self.assertEqual(self.calls,[('cp','status'),('dp','status')]);self.assertFalse(self.intent.exists())
    def test_lookup_and_apply_follow_control_protocol(self):
        import uuid
        from ffn_planed import check
        for action,payload in [('lookup',{'port':5}),('validate',self.payload)]:
            request=check(dict(v=1,id=str(uuid.uuid4()),resource='physical-ports',action=action,payload=payload))
            answer=backend.execute(request['action'],request['payload'],self.call)
            if action=='lookup':self.assertEqual(answer['config']['revision'],self.payload['revision'])
            else:self.assertTrue(answer['validated'])
    def test_start_orders_cp_before_dp_without_link_probe(self):
        self.assertTrue(backend.execute('apply',self.payload,self.call)['applied'])
        self.assertEqual(self.calls,[('cp','status'),('dp','status'),('cp','start'),('dp','start')])
        self.assertEqual(json.loads(self.intent.read_text())['port'],5)
    def test_stale_generation_never_mutates(self):
        self.path.write_text('<config/>')
        with self.assertRaisesRegex(ValueError,'changed'):backend.execute('apply',self.payload,self.call)
        self.assertEqual(self.calls,[('cp','status'),('dp','status')])
    def test_dp_failure_withdraws_only_owned_cp_redirect(self):
        self.fail=True
        with self.assertRaisesRegex(RuntimeError,'DP rejected'):backend.execute('apply',self.payload,self.call)
        self.assertEqual(self.calls[-1],('cp','stop'))


if __name__=='__main__':unittest.main()
