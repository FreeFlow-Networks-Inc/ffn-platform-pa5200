import unittest
from ffn_lacp_packets import decode,Observations

# Ethernet + actor/partner/collector/terminator TLVs in network byte order.
FRAME=bytes.fromhex('0180c2000002 020000000001 8809 0101 '
    '0114 8000 020000000001 0001 8000 0017 3f 000000 '
    '0214 8000 020000000002 0001 8000 0017 3f 000000 '
    '0310 0000 000000000000000000000000 0000')+bytes(50)


class PacketTests(unittest.TestCase):
    def test_wire_layout_and_peer_identity(self):
        self.assertEqual(len(FRAME),124)
        pdu=decode(FRAME)
        self.assertEqual(pdu['actor']['system'],'02:00:00:00:00:01')
        self.assertEqual(pdu['partner']['system'],'02:00:00:00:00:02')
        self.assertEqual(pdu['actor']['port'],23)
        self.assertEqual(pdu['actor']['system_priority'],32768)
        self.assertTrue(pdu['actor']['flags']['collecting'])
        self.assertFalse(pdu['actor']['flags']['expired'])

    def test_truncation_wrong_protocol_and_malformed_tlvs(self):
        for n in (0,14,60,110,123):
            with self.assertRaises(ValueError):decode(FRAME[:n])
        for offset,value in ((0,0),(6,3),(12,0x81),(14,2),(15,2),(16,2),(17,19),
                             (36,1),(37,19),(56,4),(57,15),(72,1),(73,1),(31,0)):
            changed=bytearray(FRAME);changed[offset]=value
            with self.subTest(offset=offset),self.assertRaises(ValueError):decode(bytes(changed))

    def test_expiry_uses_local_preference_and_never_claims_negotiation(self):
        observed=Observations();frame=bytearray(FRAME);frame[32]&=~2
        self.assertTrue(observed.receive(23,bytes(frame),10))
        row=observed.snapshot(12.9)['ports'][0]
        self.assertEqual(row['source'],'02:00:00:00:00:01')
        self.assertFalse(row['expired']);self.assertFalse(row['negotiated']);self.assertFalse(row['forwarding_verified'])
        self.assertTrue(observed.snapshot(13)['ports'][0]['expired'])
        self.assertTrue(observed.snapshot(9)['ports'][0]['expired'])
        frame[52]&=~2;observed.receive(23,bytes(frame),20)
        self.assertFalse(observed.snapshot(109)['ports'][0]['expired'])
        self.assertTrue(observed.snapshot(110)['ports'][0]['expired'])
        self.assertEqual(observed.snapshot(20)['ports'][0]['received'],2)

    def test_invalid_frames_do_not_refresh_partner_and_ports_are_separate(self):
        observed=Observations();observed.receive(23,FRAME,0);observed.receive(24,FRAME,2)
        self.assertFalse(observed.receive(23,b'bad',2))
        rows=observed.snapshot(3)['ports']
        self.assertTrue(rows[0]['expired']);self.assertFalse(rows[1]['expired'])
        self.assertEqual(observed.invalid,1)
        with self.assertRaises(ValueError):observed.receive(25,FRAME,4)


if __name__=='__main__':unittest.main()
