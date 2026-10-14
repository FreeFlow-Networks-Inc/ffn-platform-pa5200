import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import ffn_faceplate as f

class FaceplateTests(unittest.TestCase):
    def setUp(self):
        self.optics={p:dict(present=True,tx_enabled=True,tx_disable=False,control_ready=True) for p in range(5,21)}
        inventory=patch.object(f,'sfp_inventory',side_effect=lambda:self.optics)
        apply=patch.object(f,'sfp_apply',side_effect=lambda p,e:self.optics[p].update(tx_enabled=e,tx_disable=not e))
        inventory.start();apply.start()
        self.addCleanup(inventory.stop);self.addCleanup(apply.stop)
        link=patch.object(f,'sfp_link_apply',return_value=False)
        link.start();self.addCleanup(link.stop)

    def test_mapping_validation_and_readback(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/'state.json'
            ports=[{'port':p,'enabled':True,'link':False,'speed_mb':10000} for p in f.PORTS]
            calls=[]
            def call(req):
                calls.append(req)
                if req['op']=='port.set': next(p for p in ports if p['port']==req['port'])['enabled']=req['enable']
                return {'ports':ports,'ok':True}
            with patch.object(f,'STATE',state),patch.object(f,'call',side_effect=call):
                before=f.observe()
                self.assertEqual(len(before['ports']),24)
                self.assertNotIn(12,[p['bcm_port'] for p in before['ports']])
                with self.assertRaises(ValueError): f.apply({'revision':before['revision'],'port':25,'enabled':False})
                with self.assertRaises(ValueError): f.apply({'revision':-1,'port':5,'enabled':False})
                result=f.apply({'revision':before['revision'],'port':5,'enabled':False})
                self.assertEqual(result['activation'],'verified')
                self.assertIn({'op':'port.set','port':16,'enable':False},calls)
                self.assertEqual(json.loads(state.read_text()),{'ports':{'5':False}})

    def test_copper_speed_change_reaches_the_readback_and_settles(self):
        """A copper speed set once died in the readback (link mode unbound on the copper branch) after
        the PHY was written, leaving the operation pending; it must verify and record the speed."""
        phys=[{'phy':phy,'interface':'ethernet1/%d'%front,'bcm_port':{1:28,2:13,3:14,4:15}[front],'ready':True,'enabled':True,'link':False,'speed_mbps':None,
               'configured_speed':'auto','supported_speeds':[100,1000,10000],'advertised_speeds':[100,1000,10000],
               'control_register':0,'negotiation':{'valid':True},'pair_map_register':0xe4,'pair_map_recovery':False}
              for phy,front in ((16,2),(17,1),(18,4),(19,3))]
        copper={'revision':7,'phys':phys,'saved':{}}
        def copper_apply(port,request):
            row=next(r for r in phys if r['interface']==port['name'])
            row['configured_speed']=request['speed'];row['advertised_speeds']=[100,1000,10000] if request['speed']=='auto' else [int(request['speed'])]
            return {'activation':'verified'}
        with tempfile.TemporaryDirectory() as tmp,patch.object(f,'STATE',Path(tmp)/'state.json'):
            def call(req):
                if req['op']=='port.list':return {'ports':[{'port':p,'enabled':True,'link':False,'speed_mb':10000} for p in f.PORTS]}
                return {'configured_speed':'auto','supported_speeds':[1000,10000]}
            with patch.object(f,'call',side_effect=call),patch.object(f,'copper_inventory',return_value=copper),patch.object(f,'copper_apply',side_effect=copper_apply):
                before=f.observe()
                row=next(p for p in before['ports'] if p['port']==3)
                self.assertEqual(row['media'],'copper');self.assertEqual(row['advertised_speeds'],[100,1000,10000])
                result=f.apply({'revision':before['revision'],'port':3,'speed':'1000'})
                self.assertEqual(result['activation'],'verified')
                after=next(p for p in result['data']['ports'] if p['port']==3)
                self.assertEqual(after['configured_speed'],'1000');self.assertEqual(after['advertised_speeds'],[1000])
                saved=json.loads(f.STATE.read_text())
                self.assertEqual(saved['speeds'],{'3':'1000'});self.assertNotIn('pending',saved)

    def test_speed_persistence_revision_and_readback(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(f,'STATE',Path(tmp)/'state.json'):
            speeds={p:'auto' for p in f.PORTS}
            def call(req):
                if req['op']=='port.list':return {'ports':[{'port':p,'enabled':True,'link':False,'speed_mb':10000} for p in f.PORTS]}
                if req['op']=='port.link.set':speeds[req['port']]=req['speed']
                return {'configured_speed':speeds[req['port']],'supported_speeds':[1000,10000]}
            with patch.object(f,'call',side_effect=call):
                before=f.observe()
                result=f.apply({'revision':before['revision'],'port':5,'speed':'1000'})
                self.assertEqual(result['data']['ports'][4]['configured_speed'],'1000')
                self.assertEqual(json.loads(f.STATE.read_text())['speeds'],{'5':'1000'})
                self.assertNotEqual(before['revision'],result['data']['revision'])
                with self.assertRaises(ValueError):f.apply({'revision':before['revision'],'port':5,'speed':'auto'})
                with self.assertRaises(ValueError):f.apply({'revision':f.observe()['revision'],'port':1,'speed':'1000'})

    def test_unknown_outcome_blocks_retry(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(f,'STATE',Path(tmp)/'state.json'):
            observation={'revision':1,'ports':[{'available':True,'bcm_port':28}],'saved':{'ports':{}}}
            with patch.object(f,'observe',return_value=observation),patch.object(f,'call',side_effect=OSError('timeout')):
                with self.assertRaises(OSError): f.apply({'revision':1,'port':1,'enabled':False})
                self.assertTrue(json.loads(f.STATE.read_text())['pending'])
                with self.assertRaises(ValueError): f.apply({'revision':1,'port':1,'enabled':False})

if __name__=='__main__': unittest.main()
