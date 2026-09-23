import struct
import unittest
from ffn_fe100_nexthop import encode_front
from ffn_fe100_packet_lab import Lab
from validate_front_sessions import directional_frames, vlan_return_frame, qualifies_front, expected_return
from validate_physical_sessions import checksum


class FrontEncoding(unittest.TestCase):
    def test_tcp_nat_probe_full_checksums_payload_sequence_and_ttl(self):
        for mode in ('address','port'):
            for reverse in (False,True):
                packets=directional_frames('12'*16,4,reverse,mode,'tcp')
                wanted=expected_return('12'*16,4,reverse,mode,'tcp')
                for packet,tagged in zip(packets,wanted):
                    out=tagged[:12]+tagged[16:]
                    self.assertEqual(packet[23],6);self.assertEqual(out[23],6)
                    self.assertEqual(packet[46:48],bytes.fromhex('5018'))
                    self.assertEqual(packet[38:50],out[38:50])
                    self.assertEqual(packet[54:],out[54:])
                    self.assertEqual(out[22],packet[22]-1)
                    self.assertNotEqual(packet[26:38],out[26:38])
                    for frame in (packet,out):
                        self.assertEqual(int.from_bytes(frame[16:18],'big'),len(frame)-14)
                        self.assertEqual(checksum(frame[14:34]),0)
                        pseudo=frame[26:34]+struct.pack('!BBH',0,6,len(frame)-34)
                        self.assertEqual(checksum(pseudo+frame[34:]),0)

    def test_lab_rejects_stale_failed_or_replaced_production_owners(self):
        import json,tempfile
        from pathlib import Path
        from validate_front_sessions import aggregate_owners
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'ae1-status.json'
            state=dict(state='active',applied=True,updated_monotonic=100,token='owner',pid=10,process_start='20',boot_id='boot')
            path.write_text(json.dumps(state));before=aggregate_owners(root,100)
            for changes in ({'state':'failed'},{'applied':False},{'error':'busy'},{'updated_monotonic':90}):
                path.write_text(json.dumps(dict(state,**changes)))
                with self.assertRaises(RuntimeError):aggregate_owners(root,100)
            path.write_text(json.dumps(dict(state,token='replacement')))
            self.assertNotEqual(before,aggregate_owners(root,100))

    def test_qmap_uses_translated_addresses_and_observed_queue(self):
        from ffn_fe100_packet_lab import front_qmap
        from ffn_fe100_sessions import key4,nat_entry4
        from ffn_fe100_nat_lab import tuples
        for reverse in (False,True):
            original,translated=tuples('address',reverse)
            key=key4(original['source'],original['destination'],original['source_port'],original['destination_port'],17,4094)
            flow=nat_entry4(key,1001,31,translated)
            for queue in (60,96):
                qm=front_qmap(flow,5 if reverse else 13,queue)
                self.assertEqual(qm[12:20],flow[48:56])
                self.assertNotEqual(qm[12:20],key[8:16])
                self.assertEqual(int.from_bytes(qm[:4],'big')&65535,queue)

    def test_nat_probe_roundtrip_and_independent_checksums_both_directions(self):
        from ffn_fe100_nat_lab import tuples,rewrite
        for mode in ('address','port'):
            for front5 in (False,True):
                original,translated=tuples(mode,front5)
                packets=directional_frames('12'*16,4,front5,mode)
                wanted=expected_return('12'*16,4,front5,mode)
                for packet,tagged in zip(packets,wanted):
                    output=tagged[:12]+tagged[16:]
                    for raw in (packet,output):
                        self.assertEqual(checksum(raw[14:34]),0)
                        udp=raw[34:]
                        self.assertEqual(checksum(raw[26:34]+struct.pack('!BBH',0,17,len(udp))+udp),0)
                    self.assertEqual(output[22],63)
                    self.assertEqual(output[26:],rewrite(packet,translated)[26:])
                    self.assertNotEqual(packet[26:],output[26:])
                back_in,back_out=tuples(mode,not front5)
                self.assertEqual(translated['source'],back_in['destination'])
                self.assertEqual(original['source_port'],back_out['destination_port'])

    def test_nat_lab_return_cleanup_key_tracks_translated_packet(self):
        # Import with isolated environment, without opening any hardware.
        import os,subprocess,sys,json
        from pathlib import Path
        from ffn_fe100_nat_lab import tuples
        from ffn_fe100_sessions import key4,validate_entry4
        import itertools
        for mode,(protocol,number) in itertools.product(('address','port'),(('udp',17),('tcp',6))):
            for front in ('5','13'):
                env=dict(os.environ,FFN_FE100_FRONT_RETURN=front,FFN_FE100_CROSS='1',FFN_FE100_VLAN_RETURN='1',FFN_FE100_NAT_LAB=mode,FFN_FE100_LAB_PROTOCOL=protocol)
                result=subprocess.check_output([sys.executable,'-c',
                    'import ffn_fe100_packet_lab as x,json;print(json.dumps([x.KEY.hex(),x.RETURN_KEY.hex(),x.FORWARD.hex()]))'],
                    env=env,cwd=Path(__file__).resolve().parent,text=True)
                key,returned,forward=[bytes.fromhex(x) for x in json.loads(result)]
                original,translated=tuples(mode,front=='5')
                def packed(t,zone):return key4(t['source'],t['destination'],t['source_port'],t['destination_port'],number,zone)
                self.assertEqual(key,packed(original,4094));self.assertEqual(returned,packed(translated,4093))
                self.assertEqual(validate_entry4(forward),forward)

    def test_return_capture_does_not_hide_an_unexpected_dp_copy(self):
        from test_physical_sessions import Qualification
        fixture=Qualification();fixture.setUp()
        self.assertTrue(qualifies_front(fixture.phases))
        fixture.phases['drop']['unexpected_dp_packets']=[{'raw':'unexpected copy'}]
        self.assertFalse(qualifies_front(fixture.phases))
        fixture.setUp()
        fixture.phases['hit']['capture_drops']=1
        self.assertFalse(qualifies_front(fixture.phases))
        fixture.phases['hit']['capture_drops']=0
        fixture.phases['hit']['dp_capture_drops']=1
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
