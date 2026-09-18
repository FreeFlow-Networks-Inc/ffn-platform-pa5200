"""Exercise complete PHY/MAC control for all four commissioned copper ports.

The panel assignments below are test fixtures, not a production wiring map.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import ffn_faceplate as face
import ffn_phy_control as phy
from test_phy_control import Bus


class FourCopperPorts(unittest.TestCase):
    def test_admin_and_all_speeds_are_isolated_to_each_commissioned_pair(self):
        mapping={'1':{'phy':16,'bcm_port':13},'2':{'phy':17,'bcm_port':28},
                 '3':{'phy':18,'bcm_port':15},'4':{'phy':19,'bcm_port':14}}
        bus=Bus();macs={p:True for p in face.PORTS};writes=[]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);gate=root/'gate';gate.write_text('N')
            mapfile=root/'mapping';mapfile.write_text(json.dumps(mapping))
            def inventory():return phy.inventory(bus)
            def call(request):
                if request['op']=='port.list':
                    return {'ports':[{'port':p,'enabled':enabled,'link':False,'speed_mb':10000} for p,enabled in macs.items()]}
                if request['op']=='port.set':
                    writes.append(('mac',request['port'],request['enable']))
                    macs[request['port']]=request['enable'];return {'ok':True}
                if request['op']=='port.link.status':
                    return {'configured_speed':'auto','supported_speeds':[1000,10000]}
                raise AssertionError('unexpected hardware operation: '+str(request))
            def copper_apply(port,request):
                writes.append(('phy',port['phy_address'],request.get('enabled')))
                return phy.apply(bus,{'revision':inventory()['revision'],'phy':port['phy_address'],
                                      **{k:v for k,v in request.items() if k in ('speed','enabled','restart_autoneg')}})
            with patch.object(face,'STATE',root/'faceplate'),patch.object(phy,'STATE',root/'phy'),\
                 patch.object(phy,'MAPPING',mapfile),patch.object(phy,'GATE',gate),\
                 patch.object(face,'call',side_effect=call),patch.object(face,'copper_inventory',side_effect=inventory),\
                 patch.object(face,'copper_apply',side_effect=copper_apply):
                observed=face.observe()['ports'][:4]
                self.assertTrue(all(p['admin_configuration'] and p['speed_configuration'] for p in observed))
                for front,entry in mapping.items():
                    for enabled in (False,True):
                        writes.clear();bus.writes.clear()
                        result=face.apply({'revision':face.observe()['revision'],'port':int(front),'enabled':enabled})
                        row=result['data']['ports'][int(front)-1]
                        self.assertEqual(row['mac_enabled'],enabled)
                        self.assertEqual(row['phy_enabled'],enabled)
                        expected=[('mac',entry['bcm_port'],enabled),('phy',entry['phy'],enabled)]
                        self.assertEqual(writes,expected if not enabled else expected[::-1])
                        self.assertTrue(all(address==entry['phy'] for address,_,_,_ in bus.writes))
                    for speed in ('100','1000','10000','auto'):
                        writes.clear();bus.writes.clear()
                        result=face.apply({'revision':face.observe()['revision'],'port':int(front),'speed':speed})
                        self.assertEqual(result['data']['ports'][int(front)-1]['configured_speed'],speed)
                        self.assertTrue(bus.writes)
                        self.assertTrue(all(address==entry['phy'] for address,_,_,_ in bus.writes))
                        self.assertFalse(any(operation[0]=='mac' for operation in writes))
                    bus.writes.clear()
                    face.apply({'revision':face.observe()['revision'],'port':int(front),'restart_autoneg':True})
                    self.assertEqual([(p,d,r) for p,d,r,v in bus.writes],[(entry['phy'],7,0xffe0),(entry['phy'],7,0)])
                    self.assertEqual(gate.read_text(),'N')
                saved=json.loads(face.STATE.read_text())
                self.assertEqual(saved['ports'],{p:True for p in mapping})
                self.assertEqual(saved['speeds'],{p:'auto' for p in mapping})
                self.assertNotIn('pending',saved)


if __name__=='__main__':unittest.main()
