import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from ffn_vif import Assignments,validate,protect_network
from ffn_vif_runtime import Owner


def config():
    return {'revision':0,'vifs':{
        'fv1':{'port':5,'vlan':100,'enabled':True,'network':{'mode':'l3','addresses':[]}},
        'fv2':{'port':13,'vlan':100,'enabled':True,'network':{'mode':'l3','addresses':[]}}}}


class Fake:
    def __init__(self):self.current={'revision':0,'vifs':{}};self.fail=0;self.calls=0
    def preflight(self,old,new):pass
    def reconcile(self,old,new):
        self.calls+=1
        if self.fail and self.calls<=self.fail:raise OSError('injected netdev failure')
        self.current=copy.deepcopy(new)


class Vifs(unittest.TestCase):
    def setUp(self):self.frame=bytes.fromhex('02ff0000000202ff0000000188b5')+bytes(range(46))

    def test_tag_mapping_and_unknown_isolation(self):
        a=Assignments(config(),{5,13});port,wire=a.egress('fv1',self.frame)
        self.assertEqual(port,5);self.assertEqual(wire[12:18].hex(),'8100006488b5')
        self.assertEqual(a.ingress(13,wire),('fv2',self.frame))
        self.assertIsNone(a.ingress(1,wire));self.assertIsNone(a.ingress(13,self.frame))
        for vlan in (0,101,4095):self.assertIsNone(a.ingress(13,wire[:14]+vlan.to_bytes(2,'big')+wire[16:]))
        self.assertIsNone(a.egress('fv1',wire)) # no nested tag injection

    def test_disable_reassign_and_untagged(self):
        c=config();c['vifs']['fv2']['enabled']=False
        a=Assignments(c,{5,13});wire=a.egress('fv1',self.frame)[1]
        self.assertIsNone(a.ingress(13,wire));self.assertIsNone(a.egress('fv2',self.frame))
        c['vifs']['fv1']['vlan']=None;a=Assignments(c,{5,13})
        self.assertEqual(a.ingress(5,self.frame),('fv1',self.frame))
        self.assertEqual(a.egress('fv1',self.frame),(5,self.frame))
        c['vifs']['fv1']['port']=13;a=Assignments(c,{5,13})
        self.assertIsNone(a.ingress(5,self.frame));self.assertEqual(a.egress('fv1',self.frame)[0],13)

    def test_mtu_and_nested_tag_bounds(self):
        a=Assignments(config(),{5,13});frame=self.frame[:14]+bytes(1500)
        wire=a.egress('fv1',frame)[1];self.assertEqual(len(wire),1518)
        self.assertEqual(a.ingress(13,wire),('fv2',frame))
        self.assertIsNone(a.egress('fv1',frame+b'x'));self.assertIsNone(a.ingress(13,wire+b'x'))
        self.assertIsNone(a.ingress(13,wire[:16]+b'\x81\x00'+wire[18:]))

    def test_invalid_assignments(self):
        for field,value in [('port',True),('port',25),('vlan',True),('vlan',0),('enabled',1)]:
            c=config();c['vifs']['fv1'][field]=value
            with self.assertRaises(ValueError):validate(c)
        c=config();c['vifs']['fv2']['port']=5
        with self.assertRaises(ValueError):validate(c)
        with self.assertRaises(ValueError):Assignments(config(),{5})
        c=config();c['vifs']['fv1']['network']={'mode':'l2','vlans':[1,2],'pvid':1}
        with self.assertRaises(ValueError):validate(c)

    def test_revision_persistence_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);backend=Fake();owner=Owner(backend,{5,13},path/'state',path/'intent')
            owner.replace(config());self.assertEqual(owner.config['revision'],1)
            with self.assertRaises(ValueError):owner.replace(config())
            wanted=copy.deepcopy(owner.config);wanted['vifs']['fv1']['vlan']=200
            backend.fail=2
            with self.assertRaises(OSError):owner.replace(wanted)
            self.assertEqual(owner.config['vifs']['fv1']['vlan'],100)
            self.assertFalse(owner.recovery_required)
            self.assertEqual(json.loads((path/'state').read_text()),owner.config)

    def test_failed_rollback_blocks_and_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);backend=Fake();backend.fail=2
            owner=Owner(backend,{5,13},path/'state',path/'intent')
            with self.assertRaises(OSError):owner.replace(config())
            self.assertTrue(owner.recovery_required)
            with self.assertRaises(RuntimeError):owner.replace(config())
            restarted=Owner(backend,{5,13},path/'state',path/'intent')
            self.assertTrue(restarted.recovery_required)
            restarted.recover();self.assertFalse(restarted.recovery_required)
            self.assertEqual(restarted.config['vifs'],{})

    def test_other_network_edit_preserves_vif_vrf_and_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'vifs.json';c=config()
            c['vifs']['fv1']['network']={'mode':'l3','vrf':'vrf-test','addresses':['198.18.0.1/24']}
            path.write_text(json.dumps(c))
            with self.assertRaises(ValueError):protect_network({'ports':{},'vrfs':{}},path)
            protect_network({'ports':{},'vrfs':{'vrf-test':1001}},path)
            with self.assertRaises(ValueError):protect_network({'ports':{'p1':c['vifs']['fv1']['network']},'vrfs':{'vrf-test':1001}},path)


if __name__=='__main__':unittest.main()
