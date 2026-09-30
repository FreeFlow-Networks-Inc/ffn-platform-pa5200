import struct
import unittest
from unittest.mock import patch
from ffn_fe100_sessions import key4, forwarding_entry4, validate_entry4
from ffn_fe100_session_adapter import encode_native, decode_native


class PacketSessionTests(unittest.TestCase):
    def test_repeated_readiness_uses_existing_mapping_and_fresh_status(self):
        import ffn_fe100_packet_lab as lab
        import ffn_fe100_live_sessions as live
        with patch.dict(lab.WORKER_STATE,{},clear=True),patch.object(live,'LiveSessions') as factory:
            factory.return_value.status.side_effect=[{'ready':True},{'ready':False}]
            self.assertTrue(lab.worker({'kind':'readiness'},7)['ready'])
            self.assertFalse(lab.worker({'kind':'readiness'},7)['ready'])
            factory.assert_called_once_with(False,lock_fd=7,commissioning=True)

    def setUp(self):
        self.key=key4('198.18.0.1','198.18.0.2',49000,49001,17,4094)

    def test_native_cutthrough_nexthop(self):
        wire=forwarding_entry4(self.key,1001,31,decrement_ttl=True)
        native=encode_native(wire)
        self.assertEqual(native[32:40].hex(),'940000000000001f')
        self.assertEqual(native[52:56].hex(),'000003e9')
        self.assertEqual(decode_native(native,self.key),wire)

    def test_drop_roundtrip(self):
        wire=forwarding_entry4(self.key,1001,drop=True)
        self.assertEqual(wire[16:20].hex(),'08000000')
        self.assertEqual(decode_native(encode_native(wire),self.key),wire)

    def test_reject_unreviewed_flags_and_state(self):
        wire=forwarding_entry4(self.key,1001,31)
        for offset in (20,24,28,32,40,44,48,63):
            damaged=bytearray(wire);damaged[offset]|=1
            with self.assertRaises(ValueError):validate_entry4(damaged)
        for flag in (1<<29,1<<25,1<<23,1):
            damaged=bytearray(wire)
            struct.pack_into('!I',damaged,16,int.from_bytes(wire[16:20],'big')|flag)
            with self.assertRaises(ValueError):validate_entry4(damaged)

    def test_conflicting_actions(self):
        for kwargs in ({'drop':True,'next_hop':31},{'drop':True,'decrement_ttl':True},
                       {'next_hop':65536},{'next_hop':True},{'drop':1}):
            with self.assertRaises(ValueError):forwarding_entry4(self.key,1,**kwargs)


if __name__=='__main__':unittest.main()
