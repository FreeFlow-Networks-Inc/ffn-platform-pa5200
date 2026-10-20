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
        self.cfg={'revision':1,'ports':{'p5':{'mode':'l3','vrf':'vrf-test','addresses':['198.18.0.1/24']}},'vrfs':{'vrf-test':1001}}
        for name,value in [('STATE',self.path),('REQUIRE_ATTACHMENT',False)]:
            p=patch.object(net,name,value);p.start();self.addCleanup(p.stop)

    def test_shared_engine_and_guards(self):
        self.assertEqual(net.__name__,'ffn_linux_network')
        self.assertEqual(net.MAX_PORTS,24)
        with patch.object(net,'exists',return_value=True),patch.object(net,'backend',return_value={'ports':[]}),patch.object(net,'save') as save:
            # Validation refuses a port whose VRF the request removes, before any device is touched.
            with self.assertRaisesRegex(ValueError,'undefined VRF'):
                net.patch(self.cfg,{'revision':1,'vrfs':{}})
            save.assert_not_called()

    def test_lag_member_reconfiguration_is_refused(self):
        links=[{'ifname':'p5','master':'lag1'}]
        with patch.object(net,'exists',return_value=True),patch.object(net,'backend',return_value={'ports':[5]}), \
             patch.object(net,'ip',return_value=json.dumps(links)):
            with self.assertRaisesRegex(ValueError,'deactivate the LACP group'):
                net.prepare(self.cfg,{'revision':1,'ports':{'p5':{'mode':'l3','addresses':['198.18.0.2/24']}}})


if __name__=='__main__':unittest.main()
