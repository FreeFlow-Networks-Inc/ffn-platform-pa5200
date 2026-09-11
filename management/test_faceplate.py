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
                with self.assertRaises(ValueError): f.apply({'revision':-1,'port':1,'enabled':False})
                result=f.apply({'revision':before['revision'],'port':1,'enabled':False})
                self.assertEqual(result['activation'],'verified')
                self.assertEqual(calls[-2],{'op':'port.set','port':28,'enable':False})
                self.assertEqual(json.loads(state.read_text()),{'ports':{'1':False}})

    def test_unknown_outcome_blocks_retry(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(f,'STATE',Path(tmp)/'state.json'):
            observation={'revision':1,'ports':[{'available':True,'bcm_port':28}],'saved':{'ports':{}}}
            with patch.object(f,'observe',return_value=observation),patch.object(f,'call',side_effect=OSError('timeout')):
                with self.assertRaises(OSError): f.apply({'revision':1,'port':1,'enabled':False})
                self.assertTrue(json.loads(f.STATE.read_text())['pending'])
                with self.assertRaises(ValueError): f.apply({'revision':1,'port':1,'enabled':False})

if __name__=='__main__': unittest.main()
