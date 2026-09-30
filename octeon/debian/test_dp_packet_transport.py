import struct
import socket
import collections
import unittest
from ffn_dp_packet_transport import decode, decode_otmh_ssp, encode, FRONT, OTMHDecoder, pump


class PacketTransport(unittest.TestCase):
    def test_decoder_snapshots_wiring_and_checks_current_ownership(self):
        front={5:16};cached=OTMHDecoder(front);front[5]=17
        frame=b'\0\x18\0\x10'+bytes(60)
        self.assertEqual(cached(frame,{5}),(5,bytes(60)))
        self.assertIsNone(cached(frame,{6}))
        self.assertIsNone(cached(b'\0\x18\0\x11'+bytes(60),{5}))

    def test_bursts_preserve_inspection_both_directions_and_no_empty_queue_drops(self):
        sockets=[]
        class Receiver:
            def __init__(self,sock):self.sock=sock
            def fileno(self):return self.sock.fileno()
            def setblocking(self,value):self.sock.setblocking(value)
            def recvfrom(self,n):return self.sock.recv(n),('fixture',0,0)
        class Inspector:
            def tick(self):pass
            def allow(self,port,frame):return frame[-1]!=79
        try:
            for _ in range(3):sockets.extend(socket.socketpair(socket.AF_UNIX,socket.SOCK_DGRAM))
            rx,inject,tx,collect,tap,peer=sockets
            for sock in sockets:sock.setblocking(False)
            frames=[bytes(59)+bytes([i]) for i in range(80)]
            for frame in frames:
                inject.send(b'\0\x18\0\x10'+frame);peer.send(frame)
            counts=collections.Counter()
            pump(Receiver(rx),tx,{5:tap.fileno()},Inspector(),seconds=.03,counters=counts,decoder=OTMHDecoder({5:16}))
            self.assertEqual([peer.recv(2048) for _ in range(79)],frames[:-1])
            self.assertEqual([collect.recv(2048) for _ in range(80)],[encode(5,f) for f in frames])
            self.assertEqual(counts,dict(rx_p5=79,tx_p5=80,inspection_drop=1))
        finally:
            for sock in sockets:sock.close()

    def test_short_ethernet_frame_is_padded_before_injected_headers(self):
        frame=bytes(range(42))
        wire=encode(1,frame,{1:28})
        ethernet=wire[4:16]+wire[24:]
        self.assertEqual(len(ethernet),60)
        self.assertEqual(ethernet[:42],frame)
        self.assertEqual(ethernet[42:],bytes(18))

    def test_all_ports(self):
        payload=bytes(range(60))
        self.assertEqual(len(set(FRONT.values())),20)
        self.assertFalse(set(FRONT)&{1,2,3,4})
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
