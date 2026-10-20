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
                            ('udp_fragment_sample',40),('udp_mtu_exceeded_sample',40),
                            ('misc_arp_sample',40),('misc_icmp_sample',40),
                            ('misc_ipv6_sample',80),('misc_fragment_nonfirst_sample',40),
                            ('vlan_miss_sample',56),('control_icmp6_sample',40),('control_ndp_sample',40)]:
            wire=sample(name);self.inject.send(wire);self.poll()
            self.assertEqual(self.peer.recv(4096),wire[offset:]);self.empty(self.peer)
        self.assertEqual(self.stats(),[14,0,0])

    def test_appliance_captures_reach_the_tap(self):
        """What the PA-5220 rejected on the inline WAN path, re-addressed to the harness's scope."""
        self.assertEqual(self.attach(),0)
        for name,at,zone_at in (('exception39_sample',32,None),('gre_miss_sample',48,34)):
            raw=bytearray(sample(name));raw[at:at+2]=(23<<6).to_bytes(2,'big');raw[at+4:at+6]=(23).to_bytes(2,'big')
            if zone_at is not None:raw[zone_at:zone_at+2]=(4094).to_bytes(2,'big')
            self.inject.send(raw);self.poll();self.assertEqual(self.peer.recv(4096),bytes(raw[40 if at==32 else 56:]),name);self.empty(self.peer)
        self.assertEqual(self.stats(),[2,0,0])

    def test_link_control_never_enters_data_tap(self):
        self.assertEqual(self.attach(),0)
        for name in ('control_lacp_sample','control_lldp_sample'):
            self.inject.send(sample(name));self.poll();self.empty(self.peer)
        self.assertEqual(self.stats(),[0,0,0])

    def test_native_control_normalizes_only_owned_control(self):
        from ffn_native_aggregate import Member
        members=(Member*2)(Member(23,34,0x8001),Member(24,35,0x8101))
        self.lib.ffn_aggregate_control_fe100.argtypes=[C.c_int,C.POINTER(Member),C.c_uint,C.POINTER(Binding),C.c_uint,C.c_uint64]
        self.lib.ffn_aggregate_control_receive.argtypes=[C.c_int,C.POINTER(Member),C.c_uint,C.POINTER(Binding),C.c_uint,C.c_uint64,C.c_void_p,C.c_size_t]
        binding=Binding(Scope(24,20,23,23,4094),34)
        rx,tx=socket.socketpair(socket.AF_UNIX,socket.SOCK_DGRAM)
        buffer=C.create_string_buffer(4096)
        deadline=int(time.monotonic()*1000)+2000
        receive=lambda end=deadline:self.lib.ffn_aggregate_control_receive(rx.fileno(),members,2,C.byref(binding),1,end,buffer,len(buffer))
        try:
            self.assertEqual(self.lib.ffn_aggregate_control_fe100(rx.fileno(),members,2,C.byref(binding),1,deadline),0)
            for name in ('control_lacp_sample','control_lldp_sample'):
                raw=sample(name);tx.send(raw);size=receive()
                self.assertEqual(buffer.raw[:size],b'\0\x18\0\x22'+raw[40:])
            for raw in (sample(),sample('control_ndp_sample'),sample('control_lacp_sample')[:100]):
                tx.send(raw);self.assertEqual(receive(),-1);self.assertEqual(C.get_errno(),errno.EAGAIN)
            tx.send(sample('control_lacp_sample'));self.assertEqual(receive(1),-1)
            normal=b'\0\x18\x80\x01'+sample('control_lacp_sample')[40:]
            tx.send(normal);size=receive(1)
            self.assertEqual(buffer.raw[:size],b'\0\x18\0\x22'+normal[4:])
            binding.source=35
            self.assertEqual(self.lib.ffn_aggregate_control_fe100(rx.fileno(),members,2,C.byref(binding),1,deadline),-1)
            self.assertEqual(C.get_errno(),errno.EINVAL)
        finally:rx.close();tx.close()

    def test_ipv6_tuple_and_protocol_mismatches_are_rejected(self):
        self.assertEqual(self.attach(),0)
        for offset in (32,33,36,40,56,72,74,76,78,79,92,94,98,100):
            raw=bytearray(sample('misc_ipv6_sample'));raw[offset]^=0x80
            self.inject.send(raw);self.poll();self.empty(self.peer)

    def test_stats_are_not_data_or_malformed_originals(self):
        self.assertEqual(self.attach(),0)
        source=Path(__file__).with_name('test_fe100_stats.c').read_text()
        body=re.search(r'static const char sample\[\]\s*=\s*(.*?);',source,re.S).group(1)
        wire=bytes.fromhex(''.join(re.findall(r'"([a-f0-9]+)"',body)))
        self.inject.send(wire);self.poll();self.empty(self.peer)
        self.assertEqual(self.stats(),[0,0,0])
        self.inject.send(wire[:35]);self.poll();self.empty(self.peer)
        self.assertEqual(self.stats(),[0,1,0])

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

    def filter_attach(self,fd,sources,mode,bindings):
        self.lib.ffn_packet_filter_attach.argtypes=[C.c_int,C.POINTER(C.c_uint32),C.c_uint,C.c_int,C.POINTER(Binding),C.c_uint]
        self.lib.ffn_packet_filter_attach.restype=C.c_int
        values=(C.c_uint32*len(sources))(*sources)
        array=(Binding*max(len(bindings),1))(*bindings)
        return self.lib.ffn_packet_filter_attach(fd,values,len(sources),mode,array,len(bindings))

    def test_return_filter_hands_each_owner_only_its_fronts(self):
        """The kernel filter demultiplexes the shared return port by the front port in the
        message's packet info (after an IPv4 key, an IPv6 key, or none), so one message is
        copied to one owner and a rejection means a malformed message, not another owner's."""
        rx,tx=socket.socketpair(socket.AF_UNIX,socket.SOCK_DGRAM);rx.setblocking(False)
        stats=Path(__file__).with_name('test_fe100_stats.c').read_text()
        counters=bytes.fromhex(''.join(re.findall(r'"([a-f0-9]+)"',re.search(r'static const char sample\[\]\s*=\s*(.*?);',stats,re.S).group(1))))
        try:
            self.assertEqual(self.filter_attach(rx.fileno(),[28],1,[self.binding]),0)
            for name,at in (('sample',48),('udp_miss_sample',48),('vlan_miss_sample',48),('misc_ipv6_sample',72),
                            ('ttl_sample',32),('fragment_sample',32),('misc_arp_sample',32),('control_lacp_sample',32)):
                raw=sample(name);tx.send(raw);self.assertEqual(rx.recv(4096),raw,name)
                other=bytearray(raw);other[at:at+2]=(22<<6).to_bytes(2,'big');other[at+4:at+6]=(22).to_bytes(2,'big')
                tx.send(other);self.empty(rx)                       # another owner's front: never copied here
                reserved=bytearray(raw);reserved[at]|=0x10
                tx.send(reserved);self.empty(rx)                    # reserved bits set: nobody's front
            tx.send(counters);self.empty(rx)                        # the accounting receiver's, not a data owner's
            frame=b'\0\x18\0\x1c'+bytes(60);tx.send(frame);self.assertEqual(rx.recv(4096),frame)
            tx.send(b'\0\x18\0\x1d'+bytes(60));self.empty(rx)      # ordinary frames still pass by source only
            tx.send(sample()[:30]);self.empty(rx)                   # too short to name a front
            second=Binding(Scope(24,20,22,22,4094),28)
            self.assertEqual(self.filter_attach(rx.fileno(),[28],1,[self.binding,second]),0)
            tx.send(other);self.assertEqual(rx.recv(4096),bytes(other))
            self.assertEqual(self.filter_attach(rx.fileno(),[28],1,[Binding(Scope(24,20,0,0,4094),28)]),-1)
            self.assertEqual(C.get_errno(),errno.EINVAL)
            self.assertEqual(self.filter_attach(rx.fileno(),[28],4,[self.binding]),-1)
        finally:rx.close();tx.close()

    def test_control_return_filter_passes_only_its_fronts_link_control(self):
        rx,tx=socket.socketpair(socket.AF_UNIX,socket.SOCK_DGRAM);rx.setblocking(False)
        try:
            self.assertEqual(self.filter_attach(rx.fileno(),[34,0x8001,35,0x8101],2,[self.binding]),0)
            for name in ('control_lacp_sample','control_lldp_sample'):
                raw=sample(name);tx.send(raw);self.assertEqual(rx.recv(4096),raw)
                other=bytearray(raw);other[32:34]=(22<<6).to_bytes(2,'big');tx.send(other);self.empty(rx)
            tx.send(sample());self.empty(rx)                        # a data miss never enters the LACP queue
            lacp=b'\0\x18\x80\x01'+sample('control_lacp_sample')[40:];tx.send(lacp);self.assertEqual(rx.recv(4096),lacp)
            tx.send(b'\0\x18\0\x22'+bytes(60));self.empty(rx)      # members' ordinary data stays out
        finally:rx.close();tx.close()

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
