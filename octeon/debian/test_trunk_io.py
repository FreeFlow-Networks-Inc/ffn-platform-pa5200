import copy
import unittest

from ffn_dp_packet_transport import FRONT, decode_otmh_ssp, encode
from validate_trunk_io import probe_frame, probe_mapping


class TrunkCommissioning(unittest.TestCase):
    def setUp(self):
        self.profile={'version':1,'ports':{
            '3':{'phy':19,'bcm_port':14,'packet_path_verified':False},
            '4':{'phy':18,'bcm_port':15,'packet_path_verified':False}}}

    def test_explicit_copper_mapping_does_not_certify_or_add_other_ports(self):
        original=copy.deepcopy(self.profile)
        mapping=probe_mapping([3,4],self.profile)
        self.assertEqual((mapping[3],mapping[4]),(14,15))
        self.assertFalse({1,2}&set(mapping))
        self.assertEqual(self.profile,original)
        self.assertFalse({1,2,3,4}&set(FRONT))
        self.assertEqual(probe_mapping([5,13]),FRONT)

    def test_rejects_ambiguous_or_missing_wiring(self):
        for ports in ([],[3],[3,3],[True],[25]):
            with self.subTest(ports=ports),self.assertRaises(ValueError):
                probe_mapping(ports)
        with self.assertRaises(ValueError):probe_mapping([1,3],self.profile)
        self.profile['ports']['4']['bcm_port']=14
        with self.assertRaises(ValueError):probe_mapping([3,4],self.profile)

    def test_tag_and_payload_survive_copper_envelopes(self):
        mapping=probe_mapping([3,4],self.profile)
        for size,vlan in ((64,None),(1514,None),(1518,123)):
            frame=probe_frame(3,7,size,b'nonce',vlan)
            self.assertEqual(len(frame),size)
            if vlan:self.assertEqual(frame[12:18],bytes.fromhex('8100007b88b5'))
            wire=encode(3,frame,mapping)
            self.assertEqual(wire[:4],bytes.fromhex('01000e00'))
            self.assertEqual(wire[4:16]+wire[24:],frame)
            self.assertEqual(decode_otmh_ssp(bytes.fromhex('0018000f')+frame,{3,4},mapping),(4,frame))
            self.assertIsNone(decode_otmh_ssp(bytes.fromhex('0018001c')+frame,{3,4},mapping))

    def test_rejects_invalid_probe_sizes_and_tags(self):
        for size,vlan in ((10,None),(1519,None),(64,0),(64,4095),(64,True)):
            with self.assertRaises(ValueError):probe_frame(3,0,size,b'nonce',vlan)


if __name__=='__main__':unittest.main()
