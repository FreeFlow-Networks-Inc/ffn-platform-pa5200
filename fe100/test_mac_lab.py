import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import ffn_fe100_mac_lab as lab


class MacLabTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'mac.json'
        self.patch=patch.object(lab,'STATE',self.path);self.patch.start()
        self.owner=lab.Owner(None,lambda:'epoch')
        self.ports={p:dict(port=p,enabled=0,loopback=0,speed=10000,link=0) for p in lab.PORTS}
        self.fail=None;self.operations=[]
        def query(port,operation='status'):
            if operation!='status':
                self.assertTrue(self.path.exists())
                self.assertIn(port,json.loads(self.path.read_text())['touched'])
                self.operations.append((port,operation))
                self.ports[port].update(enabled=int(operation=='begin'),loopback=int(operation=='begin'))
                if self.fail==port and operation=='begin':raise OSError('lost reply after write')
            return dict(self.ports[port])
        self.owner.query=query

    def tearDown(self):
        self.patch.stop();self.temp.cleanup()

    def test_success_restores_disabled_baseline(self):
        result=self.owner.begin();self.assertTrue(result['ready'])
        self.assertFalse(result['external_wire_verified'])
        self.owner.restore()
        self.assertTrue(all(p['enabled']==p['loopback']==0 for p in self.ports.values()))
        self.assertEqual(json.loads(self.path.read_text())['stage'],'restored')

    def test_single_port_does_not_modify_the_other_port(self):
        self.owner.ports=(7,)
        self.owner.begin();self.owner.restore()
        self.assertTrue(all(p==7 for p,op in self.operations))
        self.assertEqual(self.ports[16]['enabled'],0)

    def test_partial_write_and_lost_reply_are_recoverable(self):
        self.fail=16
        with self.assertRaises(OSError):self.owner.begin()
        self.assertEqual(json.loads(self.path.read_text())['touched'],[7,16])
        self.owner.restore()
        self.assertTrue(all(p['enabled']==p['loopback']==0 for p in self.ports.values()))

    def test_live_or_preexisting_owner_is_never_adopted(self):
        for key in ('enabled','loopback','link'):
            self.ports[7][key]=1
            with self.assertRaises(RuntimeError):self.owner.begin()
            self.ports[7][key]=0
        self.assertEqual(self.operations,[])
        self.path.write_text(json.dumps({'stage':'preparing'}))
        with self.assertRaises(RuntimeError):self.owner.begin()

    def test_hardware_restart_blocks_stale_cleanup(self):
        self.owner.begin();self.operations.clear();self.owner.epoch=lambda:'new'
        with self.assertRaises(RuntimeError):self.owner.restore()
        self.assertEqual(self.operations,[])

    def test_scope_rejects_production_member_ports(self):
        for port in (1,24,32,33):
            with self.assertRaises(ValueError):lab.recipe(port,'begin')
        with self.assertRaises(ValueError):lab.recipe(7,'speed')


if __name__=='__main__':unittest.main()
