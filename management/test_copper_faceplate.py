import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import ffn_faceplate as face

class CopperFaceplateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.patchers=[patch.object(face,'STATE',Path(self.temp.name)/'state')]
        self.mac={p:True for p in face.PORTS};self.events=[]
        self.phy={'revision':42,'saved':{},'phys':[{'phy':15+p,'bcm_port':face.PORTS[p-1],'interface':'ethernet1/'+str(p),'ready':True,'control_register':0,
            'enabled':True,'link':p==2,'speed_mbps':1000 if p==2 else None,'configured_speed':'auto','supported_speeds':[100,1000,10000]} for p in range(1,5)]}
        def sdk(req):
            if req['op']=='port.list':return {'ports':[{'port':p,'enabled':self.mac[p],'link':False,'speed_mb':10000} for p in face.PORTS]}
            if req['op']=='port.set':self.events.append(('mac',req['port'],req['enable']));self.mac[req['port']]=req['enable'];return {}
            return {'configured_speed':'10000','supported_speeds':[1000,10000]}
        def apply(port,req):
            self.events.append(('phy',port['phy_address'],req.copy()))
            row=self.phy['phys'][port['port']-1]
            if 'speed' in req:row['configured_speed']=req['speed']
            if 'enabled' in req:row['enabled']=req['enabled']
            self.phy['revision']+=1
        self.patchers += [patch.object(face,'call',side_effect=sdk),patch.object(face,'copper_inventory',side_effect=lambda:copy.deepcopy(self.phy)),patch.object(face,'copper_apply',side_effect=apply)]
        for p in self.patchers:p.start();self.addCleanup(p.stop)
    def test_all_four_mapped_ports_use_phy_speed_control(self):
        for number in range(1,5):
            before=face.observe();result=face.apply({'revision':before['revision'],'port':number,'speed':'100'})
            self.assertEqual(result['data']['ports'][number-1]['configured_speed'],'100')
        self.assertTrue(all(e[0]=='phy' for e in self.events))
    def test_disable_mac_before_phy_enable_phy_before_mac(self):
        for enabled in (False,True):
            self.events.clear();before=face.observe()
            result=face.apply({'revision':before['revision'],'port':3,'enabled':enabled})
            self.assertEqual(result['data']['ports'][2]['enabled'],enabled)
            self.assertEqual([e[0] for e in self.events],['phy','mac'] if enabled else ['mac','phy'])
    def test_external_link_is_not_switch_link(self):
        p=face.observe()['ports'][1]
        self.assertTrue(p['link']);self.assertEqual(p['speed_mbps'],1000)
        self.assertFalse(p['datapath_link']);self.assertFalse(p['forwarding_verified'])
    def test_unmapped_phy_rejects_writes(self):
        self.phy['phys'][0]['interface']=None
        before=face.observe();self.assertFalse(before['ports'][0]['speed_configuration'])
        with self.assertRaises(ValueError):face.apply({'revision':before['revision'],'port':1,'enabled':False})
        self.assertFalse(self.events)
    def test_phy_revision_participates_in_faceplate_revision(self):
        old=face.observe()['revision'];self.phy['revision']+=1
        with self.assertRaises(ValueError):face.apply({'revision':old,'port':2,'speed':'auto'})
        self.assertFalse(self.events)
    def test_failed_phy_keeps_multi_device_operation_pending(self):
        before=face.observe()
        with patch.object(face,'copper_apply',side_effect=OSError('write failed')):
            with self.assertRaises(OSError):face.apply({'revision':before['revision'],'port':3,'enabled':False})
        self.assertIn('pending',face.observe()['saved'])
        self.assertFalse(self.mac[14])
if __name__=='__main__':unittest.main()
