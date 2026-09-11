import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import ffn_copper_link as sync
class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        for obj,key,path in ((sync,'STATUS','status'),(sync,'JOURNAL','pending'),(sync.face,'STATE','config')):
            p=patch.object(obj,key,root/path);p.start();self.addCleanup(p.stop)
        self.row={'phy':17,'bcm_port':28,'interface':'ethernet1/2','ready':True,'enabled':True,'link':True,'speed_mbps':1000}
        self.mac={'port':28,'enabled':True,'speed_mbps':10000,'interface':'xfi','autoneg':False,'full_duplex':True}
        self.writes=[]
        p=patch.object(sync.phy,'inventory',side_effect=lambda bus:{'saved':{},'phys':[copy.deepcopy(self.row)]});p.start();self.addCleanup(p.stop)
        def call(req):
            if req['op']=='status':return {'state':'ready'}
            if req['op']=='port.copper.sync':
                self.assertTrue(sync.JOURNAL.exists());self.writes.append(req)
                self.mac.update(speed_mbps=req['speed_mbps'],interface='sgmii')
            return self.mac.copy()
        p=patch.object(sync.face,'call',side_effect=call);p.start();self.addCleanup(p.stop)
    def test_waits_for_stable_rate_then_changes_once(self):
        sync.reconcile(None);self.assertFalse(self.writes)
        result=sync.reconcile(None);self.assertEqual(len(self.writes),1)
        self.assertEqual(result['ports']['ethernet1/2']['state'],'synchronized')
        sync.reconcile(None);self.assertEqual(len(self.writes),1)
        self.assertFalse(sync.JOURNAL.exists())
    def test_unknown_mapping_or_disabled_phy_never_changes_mac(self):
        for key,value in (('interface',None),('enabled',False),('link',False)):
            original=self.row[key];self.row[key]=value
            sync.reconcile(None);sync.reconcile(None);self.assertFalse(self.writes)
            self.row[key]=original
    def test_uncertain_mutation_remains_blocked(self):
        sync.reconcile(None)
        original=sync.face.call.side_effect
        def call(req):
            if req['op']=='port.copper.sync':raise OSError('connection lost')
            return original(req)
        with patch.object(sync.face,'call',side_effect=call):
            with self.assertRaises(OSError):sync.reconcile(None)
        self.assertTrue(sync.JOURNAL.exists())
        self.assertEqual(sync.reconcile(None)['state'],'blocked')
        self.assertFalse(self.writes)
if __name__=='__main__':unittest.main()
