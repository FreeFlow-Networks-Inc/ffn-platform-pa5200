import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import ffn_faceplate as f

class FaceplateTests(unittest.TestCase):
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
