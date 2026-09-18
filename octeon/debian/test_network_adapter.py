"""Regression checks for platform guards after the shared-engine migration."""
import json
import copy
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
            with self.assertRaisesRegex(ValueError,'undefined VRF'):
                net.patch(self.cfg,{'revision':1,'vrfs':{}})
            with self.assertRaisesRegex(ValueError,'duplicate local IP'):
                net.patch(self.cfg,{'revision':1,'ports':{'p5':{'mode':'l3','vrf':'vrf-test','addresses':['198.18.0.1/24']}}})
            save.assert_not_called()

    def test_teardown_blocks_before_subprocess(self):
        with patch.object(net.S,'run') as run:
            with self.assertRaisesRegex(RuntimeError,'remove VIF'):
                net.run('ip','netns','delete',net.NS)
            run.assert_not_called()

    def test_vif_static_ecmp_and_policy_routes(self):
        cfg=copy.deepcopy(self.cfg)
        cfg['routes']=[{'dst':'0.0.0.0/0','dev':'fv1','via':'198.18.0.2','table':1001},
            {'dst':'198.19.0.0/16','table':1001,'nexthops':[
                {'dev':'fv1','via':'198.18.0.2'},{'dev':'fv1','via':'198.18.0.3','weight':2}]}]
        cfg['rules']=[{'from':'198.18.0.0/24','iif':'fv1','table':1001,'priority':101}]
        self.assertEqual(net.validate(cfg),cfg)
        self.assertEqual(cfg['ports'],{}) # no VIF lifecycle ownership transfer
        for field,value in [('dev','fv4094'),('via','198.18.0.1'),('via','203.0.113.2'),('table',254)]:
            bad=copy.deepcopy(cfg);bad['routes']=bad['routes'][:1];bad['routes'][0][field]=value
            with self.assertRaises(ValueError):net.validate(bad)
        bad=copy.deepcopy(cfg);bad['ports']['fv1']={'mode':'l3','addresses':[]}
        with self.assertRaises(ValueError):net.validate(bad)
        self.vif['vifs']['fv1']['enabled']=False
        self.path.with_name('vifs.json').write_text(json.dumps(self.vif))
        with self.assertRaisesRegex(ValueError,'configured l3'):net.validate(cfg)

    def test_inactive_vif_rejects_route_and_policy_installation(self):
        from ffn_vif_runtime import Linux
        requests=[{'routes':[{'dst':'198.19.0.0/16','dev':'fv1','via':'198.18.0.2','table':1001}]},
                  {'rules':[{'from':'198.18.0.0/24','iif':'fv1','table':1001,'priority':101}]}]
        with patch.object(net,'REQUIRE_ATTACHMENT',True),patch.object(net,'exists',return_value=True), \
             patch.object(net,'backend',return_value={'ports':[]}),patch.object(Linux,'verify'), \
             patch.object(Linux,'links',return_value={'fv1':{'flags':['UP']}}),patch.object(net,'save') as save:
            for request in requests:
                with self.assertRaisesRegex(ValueError,'not attached'):
                    net.patch(self.cfg,{'revision':1,**request})
            save.assert_not_called()

    def test_vif_changes_protect_saved_routes_and_policies(self):
        from ffn_vif_runtime import Linux
        old=copy.deepcopy(self.vif);new=copy.deepcopy(old);new['vifs']['fv1']['vlan']=200
        links={'fv1':{'ifalias':'ffn:vif:fv1','linkinfo':{'info_kind':'tun'}},
               'vrf-test':{'linkinfo':{'info_kind':'vrf'}}}
        for extra in [{'routes':[{'dev':'fv1'}]},{'rules':[{'iif':'fv1'}]}]:
            self.path.write_text(json.dumps({'ports':{},**extra}))
            with patch.object(net,'exists',return_value=True),patch.object(Linux,'links',return_value=links):
                with self.assertRaisesRegex(RuntimeError,'configured VIF'):
                    Linux().preflight(old,new)


if __name__=='__main__':unittest.main()
