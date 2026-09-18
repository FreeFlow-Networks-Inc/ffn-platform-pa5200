import unittest
from ffn_aggregate_offload import Offload
from ffn_aggregate_datapath import Gates


class OffloadTests(unittest.TestCase):
    def setUp(self):
        self.gates=Gates([23,24]);self.driver=Offload(1,[23,24])
        self.gates.apply({p:dict(collect=True,distribute=True) for p in (23,24)})
        self.ack=dict(tid=1,verified=True,exists=True,psc=9,ingress_metadata='physical-or-fixed-spa',members=[23,24])
        self.frame=bytes.fromhex('002304000001024feb9130b488b5')+bytes(46)

    def test_hardware_destination_and_unchanged_inner_frame(self):
        self.driver.acknowledge(self.ack,10,1);sent=[]
        self.assertTrue(self.driver.transmit(self.frame,self.gates,11,sent.append))
        self.assertEqual(sent[0][:4],bytes.fromhex('01800100'))
        self.assertEqual(sent[0][4:16]+sent[0][24:],self.frame)
        self.assertEqual(self.driver.transmitted,1)

    def test_closed_initial_expired_and_membership_mismatch_fall_back(self):
        self.assertFalse(self.driver.ready(self.gates,0))
        self.driver.acknowledge(self.ack,10,1)
        self.assertFalse(self.driver.ready(self.gates,15))
        self.gates.state[24]['distribute']=False
        self.assertFalse(self.driver.ready(self.gates,11))
        self.driver.acknowledge(dict(self.ack,members=[]),11,0)
        self.assertFalse(self.driver.ready(self.gates,12))
        self.gates.state[23]['distribute']=False
        self.assertFalse(self.driver.ready(self.gates,12))

    def test_bad_ack_withdraws_previous_permission(self):
        for change in [dict(tid=2),dict(members=[1]),dict(psc=0),dict(verified=False),dict(members=[24,23]),dict(ingress_metadata='aggregate')]:
            self.driver.acknowledge(self.ack,10,0)
            with self.assertRaises(ValueError):self.driver.acknowledge(dict(self.ack,**change),11,0)
            self.assertFalse(self.driver.ready(self.gates,11))

    def test_source_metadata_keeps_member_identity_across_empty_full_transitions(self):
        for source,physical in [(0x8001,34),(0x8101,35),(34,34),(35,35),(0x8002,0x8002)]:
            raw=b'\0\x18'+source.to_bytes(2,'big')+self.frame
            result=self.driver.ingress(raw)
            self.assertEqual(int.from_bytes(result[2:4],'big'),physical)
            self.assertEqual(result[4:],self.frame)


if __name__=='__main__':unittest.main()
