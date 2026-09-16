import struct
import unittest
from ffn_wan_probe import discover,offer,checksum,COOKIE


def response(mac,xid):
    data=bytearray(discover(mac,xid)[42:282]);data[0]=2;data[16:20]=bytes([192,0,2,10])
    data+=b'\x35\x01\x02\x36\x04\xc0\x00\x02\x01\xff'
    udp=struct.pack('!HHHH',67,68,len(data)+8,0)+data
    ip=struct.pack('!BBHHHBBH4s4s',0x45,0,len(udp)+20,9,0,64,17,0,b'\xc0\x00\x02\x01',b'\xff'*4)
    ip=ip[:10]+struct.pack('!H',checksum(ip))+ip[12:]
    return b'\xff'*6+bytes.fromhex('020000000002')+b'\x08\x00'+ip+udp


class Probe(unittest.TestCase):
    def test_discover_is_valid_bootp_and_ipv4(self):
        mac=bytes.fromhex('020000000001');packet=discover(mac,123)
        self.assertEqual(checksum(packet[14:34]),0)
        self.assertEqual(packet[42:45],b'\x01\x01\x06')
        self.assertEqual(packet[42+236:42+240],COOKIE)
        self.assertIsNone(offer(packet,mac,123))

    def test_offer_is_bound_to_mac_transaction_and_protocol(self):
        mac=bytes.fromhex('020000000001');packet=response(mac,123)
        self.assertEqual(offer(packet,mac,123),{'offered_address':'192.0.2.10','server_identifier':'192.0.2.1'})
        self.assertIsNone(offer(packet,mac,124))
        self.assertIsNone(offer(packet,bytes.fromhex('020000000002'),123))
        for pos in (14,24,34,35,36,37,42,42+236):
            bad=bytearray(packet);bad[pos]^=1
            self.assertIsNone(offer(bytes(bad),mac,123),pos)
        for length in (0,14,50,len(packet)-1):self.assertIsNone(offer(packet[:length],mac,123))

    def test_malformed_or_duplicate_options_rejected(self):
        mac=bytes.fromhex('020000000001');packet=response(mac,123)
        # Preserve IP/UDP lengths while making the final option truncated.
        bad=bytearray(packet);bad[-1]=54
        self.assertIsNone(offer(bytes(bad),mac,123))


if __name__=='__main__':unittest.main()
