"""Regression checks for platform guards after the shared-engine migration."""
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import ffn_network as net


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'network.json'
        self.cfg={'revision':1,'ports':{},'vrfs':{'vrf-test':1001}}
        self.vif={'revision':1,'vifs':{'fv1':{'port':5,'vlan':100,'enabled':True,
            'network':{'mode':'l3','vrf':'vrf-test','addresses':['198.18.0.1/24']}}}}
        self.path.with_name('vifs.json').write_text(json.dumps(self.vif))
        for name,value in [('STATE',self.path),('REQUIRE_ATTACHMENT',False)]:
            p=patch.object(net,name,value);p.start();self.addCleanup(p.stop)

    def test_shared_engine_and_vif_guards(self):
        self.assertEqual(net.__name__,'ffn_linux_network')
        self.assertEqual(net.MAX_PORTS,24)
        with patch.object(net,'exists',return_value=True),patch.object(net,'backend',return_value={'ports':[]}),patch.object(net,'save') as save:
            with self.assertRaisesRegex(ValueError,'dependent VIF'):
                net.patch(self.cfg,{'revision':1,'vrfs':{}})
            with self.assertRaisesRegex(ValueError,'already belongs to a VIF'):
                net.patch(self.cfg,{'revision':1,'ports':{'p5':{'mode':'l3','vrf':'vrf-test','addresses':['198.18.0.1/24']}}})
            save.assert_not_called()

    def test_teardown_blocks_before_subprocess(self):
        with patch.object(net.S,'run') as run:
            with self.assertRaisesRegex(RuntimeError,'remove VIF'):
                net.run('ip','netns','delete',net.NS)
            run.assert_not_called()


if __name__=='__main__':unittest.main()
