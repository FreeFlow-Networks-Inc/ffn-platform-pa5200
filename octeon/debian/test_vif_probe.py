import ipaddress
import struct
import unittest
from validate_vif_forwarding import neighbor_reply, checksum


class NeighborTests(unittest.TestCase):
    def test_arp_only_answers_reserved_target(self):
        mac=bytes.fromhex('02ff00000001');peer=bytes.fromhex('02ff00000002')
        source=ipaddress.ip_address('198.18.202.1').packed
        target=ipaddress.ip_address('198.18.202.2').packed
        frame=bytes.fromhex('ffffffffffff')+mac+bytes.fromhex('08060001080006040001')+mac+source+bytes(6)+target
        reply=neighbor_reply(frame,'198.18.202.2',peer)
        self.assertEqual(reply[:12],mac+peer)
        self.assertEqual(reply[20:22],b'\0\2')
        self.assertEqual(reply[28:32],target)
        self.assertIsNone(neighbor_reply(frame,'198.18.202.3',peer))

    def test_neighbor_advertisement_checksum_and_dad_exclusion(self):
        mac=bytes.fromhex('02ff00000001');peer=bytes.fromhex('02ff00000002')
        source=ipaddress.ip_address('2001:db8:202::1').packed
        target=ipaddress.ip_address('2001:db8:202::2').packed
        frame=bytes(6)+mac+b'\x86\xdd'+struct.pack('!IHBB',6<<28,24,58,255)+source+target+bytes([135,0])+bytes(6)+target
        reply=neighbor_reply(frame,'2001:db8:202::2',peer)
        self.assertEqual(reply[54],136)
        pseudo=target+source+struct.pack('!I3xB',len(reply)-54,58)
        self.assertEqual(checksum(pseudo+reply[54:]),0)
        self.assertIsNone(neighbor_reply(frame[:22]+bytes(16)+frame[38:],'2001:db8:202::2',peer))


if __name__=='__main__':unittest.main()
