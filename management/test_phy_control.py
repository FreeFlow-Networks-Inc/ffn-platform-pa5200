import unittest,tempfile
from pathlib import Path
from unittest.mock import patch
import ffn_phy_control as phy
class Bus:
    def __init__(self):
        self.values={};self.writes=[]
        for p in range(16,20):
            for key,value in {(1,2):0x600d,(1,3):0x84f9,(1,0):0,(30,0x400f):0x1089,(30,0x400d):0x443a,(7,0):0x1000,(7,0xffe4):0x100,(7,0xffe9):0x200,(7,0x20):0x1000}.items():self.values[(p,*key)]=value
    def transfer(self,p,d,r,value=None):
        if value is not None:self.values[p,d,r]=value;self.writes.append((p,d,r,value))
        return self.values.get((p,d,r),0)
class PhyTests(unittest.TestCase):
    def test_identity_advertisement_readback_and_gate_restoration(self):
        with tempfile.TemporaryDirectory() as temp:
            gate=Path(temp)/'gate';gate.write_text('N')
            with patch.object(phy,'STATE',Path(temp)/'state'),patch.object(phy,'GATE',gate):
                bus=Bus();before=phy.inventory(bus)
                result=phy.apply(bus,{'revision':before['revision'],'phy':16,'speed':'1000'})
                self.assertEqual(result['data']['phys'][0]['advertised_speeds'],[1000])
                self.assertEqual(gate.read_text(),'N');self.assertFalse(result['forwarding_verified'])
                self.assertTrue(all(p==16 for p,d,r,v in bus.writes))
                self.assertFalse(result['data']['saved'].get('pending'))
    def test_unknown_identity_and_stale_revision_never_write(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(phy,'STATE',Path(temp)/'state'):
            bus=Bus();bus.values[16,1,3]=0
            for revision in (-1,phy.inventory(bus)['revision']):
                with self.assertRaises(ValueError):phy.apply(bus,{'revision':revision,'phy':16,'speed':'1000'})
            self.assertFalse(bus.writes)
    def test_write_failure_keeps_pending_and_restores_gate(self):
        with tempfile.TemporaryDirectory() as temp:
            gate=Path(temp)/'gate';gate.write_text('N')
            with patch.object(phy,'STATE',Path(temp)/'state'),patch.object(phy,'GATE',gate):
                bus=Bus();revision=phy.inventory(bus)['revision'];original=bus.transfer
                def transfer(p,d,r,value=None):
                    if value is not None:raise OSError('failed')
                    return original(p,d,r)
                bus.transfer=transfer
                with self.assertRaises(OSError):phy.apply(bus,{'revision':revision,'phy':16,'speed':'1000'})
                self.assertEqual(gate.read_text(),'N');self.assertTrue(phy.inventory(bus)['saved']['pending'])
if __name__=='__main__':unittest.main()
