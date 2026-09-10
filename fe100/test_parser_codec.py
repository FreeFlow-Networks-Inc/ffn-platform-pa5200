import struct
import unittest
from ffn_fe100_parser_apply import encode


class ParserCodecTests(unittest.TestCase):
    def test_masked_key_matches_hardware_readback(self):
        entry={'pkey0_en':1,'pkey0_idx':8,'o_mask':255,'o_shift_r':1,
               'o_idx':8,'next':10,'action':1,'key':{'stage':7,'pkey0':48},
               'mask':{'stage':31,'pkey0':239}}
        self.assertEqual(encode(entry).hex(),
                         '80072000801fef00000000000000000000000000000000000044000007f8a053')

    def test_ipv4_classification_and_transition(self):
        raw=encode({'pt':1,'mode':1,'next':8,'action':2,'proto_en':1})
        settings,action=struct.unpack_from('>II',raw,24)
        self.assertEqual(settings,0x811)
        self.assertEqual(action,0x45)

    def test_rejects_unrepresentable_fields(self):
        for entry in [{'pt':4},{'mode':-1},{'key':{'stage':32}},
                      {'mask':{'pkey0':256}},{'invented_field':1},{'pt':True}]:
            with self.subTest(entry=entry),self.assertRaises(ValueError): encode(entry)


if __name__=='__main__': unittest.main()
