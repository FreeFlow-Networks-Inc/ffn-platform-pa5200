import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import ffn_copper_identify as identify
from test_phy_control import Bus


class Identification(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        for obj,key,name in ((identify,'PROBE','probe'),(identify,'BOOT','boot'),
                             (identify.phy,'MAPPING','map'),(identify.phy,'STATE','phy'),(identify.face,'STATE','face')):
            patcher=patch.object(obj,key,root/name);patcher.start();self.addCleanup(patcher.stop)
        identify.BOOT.write_text('boot-A')
        identify.phy.MAPPING.write_text(json.dumps({'2':{'phy':17,'bcm_port':28}}))
        self.bus=Bus()
        for address in (16,18,19):self.bus.values[address,30,0x400d]=0
        self.now=100.;self.owner=identify.Identifier(self.bus,clock=lambda:self.now,sleep=lambda _:None)
        patcher=patch.object(identify.face,'call',return_value={'ports':[{'port':p} for p in (13,28,15,14)]})
        patcher.start();self.addCleanup(patcher.stop)

    def begin(self,port=1):
        request={'revision':self.owner.status()['config']['revision'],'operation':'begin','port':port}
        self.owner.execute(request,False);self.assertFalse(identify.PROBE.exists())
        return self.owner.execute(request,True)

    def confirm(self,status):
        return {'revision':status['config']['revision'],'operation':'confirm','token':status['pending']['token']}

    def test_identifies_all_remaining_ports_without_hardware_writes(self):
        for port,address,mac in ((1,16,13),(3,18,15),(4,19,14)):
            state=self.begin(port);before=identify.phy.MAPPING.read_bytes()
            self.bus.values[address,30,0x400d]=0x30
            status=self.owner.status();self.assertEqual(status['candidate'],{'phy':address,'bcm_port':mac})
            self.owner.execute(self.confirm(state),False)
            self.assertEqual(identify.phy.MAPPING.read_bytes(),before)
            result=self.owner.execute(self.confirm(state),True)
            self.assertEqual(result['config']['mapping'][str(port)],{'phy':address,'bcm_port':mac})
            self.assertEqual(result['config']['mapping']['2'],{'phy':17,'bcm_port':28})
            with self.assertRaises(ValueError):self.owner.execute(self.confirm(state),True)
        self.assertTrue(result['complete']);self.assertEqual(self.bus.writes,[])

    def test_no_link_multiple_links_and_wan_changes_never_map(self):
        status=self.begin();initial=identify.phy.MAPPING.read_bytes()
        for changes in ({},{16:0x30,18:0x30},{17:0,16:0x30}):
            saved=copy.deepcopy(self.bus.values)
            for address,value in changes.items():self.bus.values[address,30,0x400d]=value
            with self.assertRaises(ValueError):self.owner.execute(self.confirm(status),True)
            self.assertEqual(identify.phy.MAPPING.read_bytes(),initial)
            self.bus.values=saved

    def test_existing_port_stale_revision_expiry_and_boot_change_rejected(self):
        with self.assertRaises(ValueError):self.begin(2)
        status=self.begin()
        with self.assertRaises(ValueError):self.owner.execute(self.confirm(status)|{'revision':-1},True)
        self.now+=901
        with self.assertRaises(ValueError):self.owner.execute(self.confirm(status),True)
        self.now=100
        identify.BOOT.write_text('boot-B')
        with self.assertRaises(ValueError):self.owner.execute(self.confirm(status),True)

    def test_configuration_drift_and_mac_alias_prevent_commissioning(self):
        status=self.begin();self.bus.values[16,30,0x400d]=0x30
        self.bus.values[16,7,0xffe9]=0
        with self.assertRaises(ValueError):self.owner.execute(self.confirm(status),True)
        self.bus.values[16,7,0xffe9]=0x200
        with patch.object(identify.face,'call',return_value={'ports':[]}):
            with self.assertRaises(ValueError):self.owner.execute(self.confirm(status),True)
        self.assertEqual(len(identify.phy.port_mapping()),1)

    def test_cancel_and_pending_hardware_operation(self):
        status=self.begin()
        self.owner.execute(self.confirm(status)|{'operation':'cancel'},True)
        self.assertIsNone(self.owner.status()['pending'])
        identify.phy.STATE.write_text(json.dumps({'pending':{'phy':17}}))
        with self.assertRaises(ValueError):self.owner.status()


if __name__=='__main__':unittest.main()
