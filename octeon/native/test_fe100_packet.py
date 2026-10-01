"""FE100 originals must traverse the existing native software-policy path."""
import ctypes as C
import errno
from pathlib import Path
import re
import socket
import time
import unittest
from test_packet import NativePacketTests
from ffn_native_packet import Fe100Binding as Binding,Fe100Scope as Scope,PacketOwner
from types import SimpleNamespace


def sample(name='sample'):
    source=Path(__file__).with_name('test_fe100_punt.c').read_text()
    body=re.search(r'static const char '+name+r'\[\]\s*=\s*(.*?);',source,re.S).group(1)
    return bytes.fromhex(''.join(re.findall(r'"([a-f0-9]+)"',body)))


class PuntTests(NativePacketTests):
    def setUp(self):
        super().setUp()
        self.lib.ffn_packet_fe100.argtypes=[C.c_void_p,C.POINTER(Binding),C.c_uint,C.c_uint64]
        self.lib.ffn_packet_fe100.restype=C.c_int
        self.lib.ffn_packet_fe100_stats.argtypes=[C.c_void_p,C.POINTER(C.c_uint64),C.c_uint]
        self.binding=Binding(Scope(24,20,23,23,4094),28)

    def attach(self,duration=2000):
        return self.lib.ffn_packet_fe100(self.ctx,C.byref(self.binding),1,int(time.monotonic()*1000)+duration)

    def stats(self):
        values=(C.c_uint64*3)()
        self.assertEqual(self.lib.ffn_packet_fe100_stats(self.ctx,values,3),0)
        return list(values)

    def test_disabled_by_default_and_exact_original_delivery(self):
        wire=sample();self.inject.send(wire);self.poll();self.empty(self.peer)
        self.assertEqual(self.attach(),0)
        for name,offset in [('sample',56),('ttl_sample',40),('fragment_sample',40),
                            ('udp_miss_sample',56),('udp_ttl_expired_sample',40),
                            ('udp_fragment_sample',40),('udp_mtu_exceeded_sample',40)]:
            wire=sample(name);self.inject.send(wire);self.poll()
            self.assertEqual(self.peer.recv(4096),wire[offset:]);self.empty(self.peer)
        self.assertEqual(self.stats(),[7,0,0])

    def test_short_ethernet_padding_and_malformed_lengths(self):
        self.assertEqual(self.attach(),0)
        wire=bytearray(sample('udp_miss_sample')[:56+60])
        wire[50:52]=(0x4000|60).to_bytes(2,'big')
        wire[72:74]=(28).to_bytes(2,'big')
        wire[94:96]=(8).to_bytes(2,'big')
        self.inject.send(wire);self.poll();self.assertEqual(self.peer.recv(4096),wire[56:])
        # Declared IPv4 or UDP lengths cannot run past the captured frame.
        for at,value in [(72,1000),(94,1000)]:
            bad=bytearray(wire);bad[at:at+2]=value.to_bytes(2,'big')
            self.inject.send(bad);self.poll();self.empty(self.peer)
        self.inject.send(bytes(9000));self.poll();self.empty(self.peer)
        self.assertEqual(self.stats(),[1,2,0])

    def test_returned_packets_are_inspected(self):
        self.configure();self.assertEqual(self.attach(),0)
        wire=bytearray(sample());wire[-1]=2
        self.inject.send(wire);self.poll();self.empty(self.peer)
        self.assertEqual(self.counts()['inspection_drop'],1)
        self.assertEqual(self.stats(),[1,0,0])

    def test_scope_and_expiry_reject_without_changing_normal_path(self):
        self.assertEqual(self.attach(50),0)
        wire=bytearray(sample());wire[53]^=1
        self.inject.send(wire);self.poll();self.empty(self.peer)
        time.sleep(.06);self.inject.send(sample());self.receive(bytes(60));self.poll()
        self.assertEqual(self.peer.recv(4096),bytes(60));self.empty(self.peer)
        self.assertEqual(self.stats(),[0,1,1])
        self.assertEqual(self.lib.ffn_packet_fe100(self.ctx,None,0,0),0)
        self.inject.send(sample());self.poll();self.empty(self.peer)

    def test_update_requires_barrier_and_cannot_change_owner_mapping(self):
        self.start_workers();self.assertEqual(self.attach(),0)
        self.lib.ffn_packet_resume(self.ctx)
        self.assertEqual(self.attach(),-1);self.assertEqual(C.get_errno(),errno.EBUSY)
        self.lib.ffn_packet_pause(self.ctx)
        self.binding.source=29
        self.assertEqual(self.attach(),-1);self.assertEqual(C.get_errno(),errno.EINVAL)
        self.binding.source=28;self.binding.scope.return_port=28
        self.assertEqual(self.attach(),-1)

    def test_control_wrapper_rejects_wrapping_and_unowned_fronts(self):
        owner=SimpleNamespace(lib=self.lib,handle=self.ctx,port=23)
        good=dict(trunk=24,return_port=20,front=23,in_lif=23,zone=4094,source=28)
        deadline=int(time.monotonic()*1000)+2000
        for change in ({'front':22},{'zone':65536},{'source':True},{'in_lif':-1}):
            with self.assertRaises(ValueError):PacketOwner.fe100(owner,[good|change],deadline)
        PacketOwner.fe100(owner,[good],deadline)
        self.inject.send(sample());self.poll();self.assertEqual(self.peer.recv(4096),sample()[56:])
        PacketOwner.fe100(owner,[],0)
        self.inject.send(sample());self.poll();self.empty(self.peer)


# Reuse the transport harness, without re-running its unrelated test methods.
for _name in list(vars(NativePacketTests)):
    if _name.startswith('test_') and _name not in vars(PuntTests):
        setattr(PuntTests,_name,None)
del NativePacketTests
from test_aggregate import AggregateTests


class PuntAggregateTests(AggregateTests):
    def setUp(self):
        super().setUp()
        self.lib.ffn_packet_fe100.argtypes=[C.c_void_p,C.POINTER(Binding),C.c_uint,C.c_uint64]
        self.lib.ffn_packet_fe100.restype=C.c_int
        self.binding=Binding(Scope(24,20,11,11,4094),41)

    def wire(self):
        raw=bytearray(sample())
        raw[48:50]=(11<<6).to_bytes(2,'big');raw[52:54]=(11).to_bytes(2,'big')
        return raw

    def attach(self):
        return self.lib.ffn_packet_fe100(self.ctx,C.byref(self.binding),1,int(time.monotonic()*1000)+2000)

    def test_punts_obey_member_collection_and_inspection(self):
        self.assertEqual(self.attach(),0);wire=self.wire()
        self.inject.send(wire);self.poll();self.assertEqual(self.peer.recv(4096),wire[56:])
        self.gates(collect=2,distribute=2)
        self.inject.send(wire);self.poll();self.empty(self.peer)
        self.assertEqual(self.stats()[0],1)
        self.gates(inspect=1);wire[-1]=2
        self.inject.send(wire);self.poll();self.empty(self.peer)
        self.assertEqual(self.stats()[7],1)

    def test_front_source_mapping_cannot_cross_members(self):
        self.binding.source=42
        self.assertEqual(self.attach(),-1);self.assertEqual(C.get_errno(),errno.EINVAL)
        self.binding.source=41;self.assertEqual(self.attach(),0)
        raw=self.wire();raw[48:50]=(19<<6).to_bytes(2,'big')
        self.inject.send(raw);self.poll();self.empty(self.peer)


for _name in list(vars(AggregateTests)):
    if _name.startswith('test_') and _name not in vars(PuntAggregateTests):
        setattr(PuntAggregateTests,_name,None)
del AggregateTests
if __name__=='__main__':unittest.main()
