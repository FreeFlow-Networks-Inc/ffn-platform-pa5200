"""Offline packet fixtures for the native runtime; never production traffic."""
import ctypes as C
import os
from pathlib import Path
import socket
import struct
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'debian'))
from ffn_native_packet import library,checked,COUNTERS


class NativePacketTests(unittest.TestCase):
    def setUp(self):
        self.lib=library(os.environ.get('FFN_PACKET_LIB',str(Path(__file__).with_name('libffn-packet.so'))))
        self.scan=C.CDLL(str(Path(__file__).with_name('test-scan.so')))
        self.sockets=[]
        for _ in range(3):self.sockets.extend(socket.socketpair(socket.AF_UNIX,socket.SOCK_DGRAM))
        self.rx,self.inject,self.tx,self.collect,self.tap,self.peer=self.sockets
        for s in self.sockets:s.setblocking(False)
        self.ctx=self.lib.ffn_packet_adopt(self.rx.fileno(),self.tx.fileno(),self.tap.fileno(),28)
        self.assertTrue(self.ctx)
        self.addCleanup(self.close)
    def close(self):
        self.lib.ffn_packet_close(self.ctx)
        for sock in self.sockets:sock.close()
    def poll(self):checked(self.lib.ffn_packet_poll(self.ctx,10))
    def counts(self):
        values=(C.c_uint64*16)();checked(self.lib.ffn_packet_counters(self.ctx,values,16))
        return dict(zip(COUNTERS,values))
    def receive(self,frame):self.inject.send(b'\0\x18\0\x1c'+frame)
    def empty(self,sock):
        with self.assertRaises(BlockingIOError):sock.recv(4096)
    def configure(self,v4=b'',v6=b'',scan=True):
        checked(self.lib.ffn_packet_configure(self.ctx,v4,len(v4)//4,v6,len(v6)//16,
            C.c_void_p(1) if scan else None,C.cast(self.scan.test_scan,C.c_void_p) if scan else None))
    def test_bidirectional_bursts_byte_exact_and_short_padding(self):
        frames=[bytes(12)+b'\x88\xb5'+bytes(27)+bytes([i]) for i in range(80)]
        for frame in frames:self.receive(frame);self.peer.send(frame)
        self.poll()
        for frame in frames:
            self.assertEqual(self.peer.recv(4096),frame)
            self.assertEqual(self.collect.recv(4096),b'\1\0\x1c\0'+frame[:12]+bytes(8)+frame[12:]+bytes(60-len(frame)))
        self.assertEqual(self.counts()['rx'],80);self.assertEqual(self.counts()['tx'],80)
        self.assertEqual(self.counts()['backpressure_drop'],0)
    def test_invalid_envelopes_lengths_and_source(self):
        for size in range(18):self.inject.send(bytes(size))
        self.inject.send(b'\0\x18\0\x1d'+bytes(60))
        self.receive(bytes(1519));self.receive(bytes(1518));self.poll()
        self.assertEqual(self.peer.recv(4096),bytes(1518));self.empty(self.peer)
        self.assertEqual(self.counts()['envelope_rejected'],20)
    def test_native_inspection_verdicts(self):
        self.configure()
        for verdict in (0,1,2,254,255):self.receive(bytes(59)+bytes([verdict]))
        self.poll()
        self.assertEqual([self.peer.recv(4096)[-1] for _ in range(4)],[0,1,254,255])
        self.empty(self.peer)
        for name in ('no_match','alert','block','unsupported_pass','malformed_pass','inspection_drop'):
            self.assertEqual(self.counts()[name],1)
    def test_local_services_and_config_replacement(self):
        frame=bytes(12)+b'\x08\0\x45'+bytes(15)+bytes([192,0,2,1])+bytes(25)+b'\2'
        self.configure(v4=bytes([192,0,2,1]));self.receive(frame);self.poll()
        self.assertEqual(self.peer.recv(4096),frame)
        self.configure(v4=bytes([192,0,2,2]));self.receive(frame);self.poll();self.empty(self.peer)
        self.assertEqual(self.counts()['local_input'],1);self.assertEqual(self.counts()['block'],1)
    def test_ipv6_local_services(self):
        address=bytes.fromhex('20010db8000000000000000000000001')
        frame=bytes(12)+b'\x86\xdd\x60'+bytes(23)+address+bytes(5)+b'\2'
        self.configure(v6=address);self.receive(frame);self.poll()
        self.assertEqual(self.peer.recv(4096),frame);self.assertEqual(self.counts()['local_input'],1)
    def test_unknown_inspection_verdict_fails_closed(self):
        self.configure();self.receive(bytes(59)+b'\x63')
        with self.assertRaises(OSError):self.poll()
        self.empty(self.peer);self.assertEqual(self.counts()['invalid_verdict'],1)
    def test_invalid_configuration_retains_previous(self):
        self.configure()
        self.assertEqual(self.lib.ffn_packet_configure(self.ctx,b'',65,b'',0,None,None),-1)
        self.receive(bytes(59)+b'\2');self.poll();self.empty(self.peer)
        self.assertEqual(self.counts()['block'],1)
    def test_real_appliance_inline_engine_abi(self):
        lib=C.CDLL(str(Path(__file__).with_name('libffn-inline.so')))
        lib.ffn_inline_create.argtypes=[C.c_char_p,C.c_uint,C.c_int];lib.ffn_inline_create.restype=C.c_void_p
        lib.ffn_inline_destroy.argtypes=[C.c_void_p]
        handle=lib.ffn_inline_create(b'FFN_TEST_DENY',13,2);self.assertTrue(handle)
        try:
            checked(self.lib.ffn_packet_configure(self.ctx,b'',0,b'',0,handle,C.cast(lib.ffn_inline_scan,C.c_void_p)))
            for payload in (b'clean',b'FFN_TEST_DENY'):
                udp=struct.pack('!HHHH',1000,2000,len(payload)+8,0)+payload
                ip=struct.pack('!BBHHHBBH4s4s',0x45,0,len(udp)+20,0,0,64,17,0,bytes(4),bytes(4))
                self.receive(bytes(12)+b'\x08\0'+ip+udp)
            self.poll();self.assertTrue(self.peer.recv(4096).endswith(b'clean'));self.empty(self.peer)
            self.assertEqual(self.counts()['no_match'],1);self.assertEqual(self.counts()['block'],1)
        finally:
            self.configure(scan=False);lib.ffn_inline_destroy(handle)
    def test_no_python_callbacks_or_packet_functions_in_physical_owner(self):
        import ast
        source=(Path(__file__).resolve().parents[1]/'debian/ffn_wan_runtime.py').read_text()
        tree=ast.parse(source)
        forbidden={'recv','recvfrom','send','sendto','pump','allow','encode','decode','ioctl'}
        self.assertFalse([n.func.attr for n in ast.walk(tree) if isinstance(n,ast.Call)
                          and isinstance(n.func,ast.Attribute) and n.func.attr in forbidden])

if __name__=='__main__':unittest.main()
