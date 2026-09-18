import struct
import unittest
from ffn_fe100_nexthop import encode_front
from ffn_fe100_packet_lab import Lab
from validate_front_sessions import directional_frames, vlan_return_frame, qualifies_front
from validate_physical_sessions import checksum


class FrontEncoding(unittest.TestCase):
    def test_return_capture_does_not_hide_an_unexpected_dp_copy(self):
        from test_physical_sessions import Qualification
        fixture=Qualification();fixture.setUp()
        self.assertTrue(qualifies_front(fixture.phases))
        fixture.phases['drop']['unexpected_dp_packets']=[{'raw':'unexpected copy'}]
        self.assertFalse(qualifies_front(fixture.phases))

    def test_front_destination_is_a_lif_not_cpu_sysport(self):
        raw=encode_front(31,dmac='02:52:20:ab:cd:ee')
        flags,lif,vlan,mtu,mac=struct.unpack('>IHHH6s',raw)
        self.assertEqual((flags,lif,vlan,mtu,mac.hex()),(0x810000,31,0,1518,'025220abcdee'))
        for invalid in (True,-1,65536):
            with self.assertRaises(ValueError):encode_front(invalid)

    def test_qmap_readback_ignores_only_generated_fields(self):
        wanted=bytearray(84)
        struct.pack_into('>III',wanted,0,0x02020024,(13<<6)|1,31)
        readback=bytearray(wanted)
        readback[0]|=0xfc;readback[4]|=0x80;readback[7]&=0xfc
        self.assertEqual(Lab.payload('qm',wanted.hex()),Lab.payload('qm',readback.hex()))
        for offset in (1,2,3,5,8,12,19,24,35):
            changed=bytearray(readback);changed[offset]^=1
            self.assertNotEqual(Lab.payload('qm',wanted.hex()),Lab.payload('qm',changed.hex()))

    def test_reverse_probe_preserves_valid_ip_and_udp_checksums(self):
        for reverse in (False,True):
            for frame in directional_frames('1234567890abcdef'*2,4,reverse):
                ip=frame[14:34];udp=frame[34:]
                self.assertEqual(checksum(ip),0)
                self.assertEqual(checksum(ip[12:20]+struct.pack('!BBH',0,17,len(udp))+udp),0)
                self.assertEqual(struct.unpack('!HH',udp[:4]),(49001,49000) if reverse else (49000,49001))

    def test_tagged_return_has_reserved_vlan_and_decremented_ttl(self):
        original=directional_frames('1234567890abcdef'*2,1)[0]
        frame=vlan_return_frame(original)
        self.assertEqual(frame[12:18],bytes.fromhex('81000fa00800'))
        self.assertEqual(frame[26],63)
        self.assertEqual(checksum(frame[18:38]),0)
        raw=encode_front(31,vlan=4000)
        self.assertEqual(int.from_bytes(raw[:4],'big'),0x840000)
        self.assertEqual(raw[6:8],bytes.fromhex('0fa0'))


if __name__=='__main__':unittest.main()
