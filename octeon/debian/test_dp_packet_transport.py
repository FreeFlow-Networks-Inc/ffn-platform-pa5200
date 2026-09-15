import struct
import unittest
from ffn_dp_packet_transport import decode, decode_otmh_ssp, encode, FRONT


class PacketTransport(unittest.TestCase):
    def test_all_ports(self):
        payload=bytes(range(60))
        self.assertEqual(len(set(FRONT.values())),24)
        for port,bcm in FRONT.items():
            cmh=bytearray(32); cmh[0]=16; cmh[3]=3
            struct.pack_into('!HH',cmh,24,port<<6,len(payload))
            self.assertEqual(decode(cmh+payload,FRONT),(port,payload))
            self.assertEqual(encode(port,payload)[:4],b'\x01'+struct.pack('!H',bcm)+b'\0')
            self.assertIsNone(decode(cmh+payload,{}))

    def test_control_exception_and_truncation_rejected(self):
        packet=bytearray(92); packet[0]=16; packet[3]=3
        struct.pack_into('!HH',packet,24,5<<6,60)
        for length in range(92): self.assertIsNone(decode(packet[:length],FRONT))
        for kind in (0,1,2,4,10,11,15,16,17,18,128):
            packet[3]=kind
            self.assertIsNone(decode(packet,FRONT))
        packet[3]=3; packet[23]=1
        self.assertIsNone(decode(packet,FRONT))

    def test_egress_validation(self):
        for port,frame in [(True,bytes(60)),(0,bytes(60)),(25,bytes(60)),(1,bytes(1519))]:
            with self.assertRaises(ValueError): encode(port,frame)

    def test_otmh_source_and_length(self):
        # Captured header/payload from front5 -> DAC -> front13 -> BCM24;
        # exclude the double-generated CRC exposed by the initial PKO build.
        packet=bytes.fromhex('0018000702ff0000000202ff0000000188b546464e2d5452554e4b2d544553543ac203015dfcac4361bd884bd7169e8d7305000000000000000000000000000000000000ade52b39')
        packet=packet[:-4]
        self.assertEqual(decode_otmh_ssp(packet,FRONT),(13,packet[4:]))
        self.assertIsNone(decode_otmh_ssp(packet,{5}))
        self.assertIsNone(decode(packet,FRONT))
        for n in range(18):
            self.assertIsNone(decode_otmh_ssp(packet[:n],FRONT))
        for i in (0,1,2,3):
            changed=bytearray(packet); changed[i]^=1
            self.assertIsNone(decode_otmh_ssp(changed,{5,13}))


if __name__=='__main__': unittest.main()
