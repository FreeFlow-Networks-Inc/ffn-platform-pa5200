"""Native aggregate packet and withdrawal fixtures; no physical ports touched."""
import ctypes as C
import os
from pathlib import Path
import socket
import struct
import sys
import time
import unittest
from collections import Counter
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'debian'))
from ffn_native_aggregate import aggregate_library,Member,Unit,units,AggregateOwner
from ffn_native_packet import checked
from ffn_aggregate_datapath import Gates


def frame(kind=b'\x88\xb5',tail=0):return bytes(12)+kind+bytes(45)+bytes([tail])
def tagged(data,tag=123):return data[:12]+b'\x81\x00'+struct.pack('!H',tag)+data[12:]
def wire(data,source):return b'\0\x18'+struct.pack('!H',source)+data
def encoded(data,source):return b'\1'+struct.pack('!H',source)+b'\0'+data[:12]+bytes(8)+data[12:]+bytes(max(0,60-len(data)))


class AggregateTests(unittest.TestCase):
    def setUp(self):
        self.lib=aggregate_library(str(Path(__file__).with_name('libffn-packet.so')))
        self.scan=C.CDLL(str(Path(__file__).with_name('test-scan.so')))
        self.sockets=[]
        for _ in range(3):self.sockets.extend(socket.socketpair(socket.AF_UNIX,socket.SOCK_DGRAM))
        self.rx,self.inject,self.tx,self.collect,self.tap,self.peer=self.sockets
        for sock in self.sockets:sock.setblocking(False)
        self.members=(Member*2)(Member(11,41,0x8001),Member(19,42,0x8101))
        self.ctx=self.lib.ffn_aggregate_adopt(self.rx.fileno(),self.tx.fileno(),self.tap.fileno(),self.members,2,1)
        self.assertTrue(self.ctx);self.addCleanup(self.close)
        self.network();self.gates()
    def close(self):
        self.lib.ffn_packet_close(self.ctx)
        for sock in self.sockets:sock.close()
    def network(self,parent=True,tag=123,local=()):
        network=dict(enabled=parent,mtu=1500,units=[dict(name='ae1.7',tag=tag,mtu=1500,addresses=['192.0.2.1/24'])])
        rows,_=units('ae1',network,local)
        checked(self.lib.ffn_aggregate_network(self.ctx,rows,len(rows)))
    def gates(self,collect=3,distribute=3,inspect=0,ttl=5,offload=0,ready=True):
        now=time.monotonic()*1000
        checked(self.lib.ffn_aggregate_gates(self.ctx,collect,distribute,inspect,int(now+ttl*1000),int(now+offload*1000) if offload else 0,int(ready),
            C.c_void_p(1) if inspect else None,C.cast(self.scan.test_scan,C.c_void_p) if inspect else None))
    def poll(self):checked(self.lib.ffn_packet_poll(self.ctx,10))
    def empty(self,sock):
        with self.assertRaises(BlockingIOError):sock.recv(4096)
    def stats(self):
        out=(C.c_uint64*8)();checked(self.lib.ffn_aggregate_stats(self.ctx,out,8));return list(out)
    def test_vlan_and_alias_delivery_and_unit_counters(self):
        value=tagged(frame())
        for source in (41,42,0x8001,0x8101):self.inject.send(wire(value,source))
        self.poll()
        for _ in range(4):self.assertEqual(self.peer.recv(4096),value)
        out=(C.c_uint64*4)();checked(self.lib.ffn_aggregate_unit_stats(self.ctx,123,out,4))
        self.assertEqual(list(out),[4,4*len(value),0,0])
    def test_link_only_parent_and_unknown_nested_vlan_drop(self):
        self.network(parent=False)
        for value in (frame(),tagged(frame(),124),tagged(tagged(frame())),tagged(frame(),0),frame(b'\x88\xa8')):
            self.inject.send(wire(value,41));self.peer.send(value)
        self.poll();self.empty(self.peer);self.empty(self.collect);self.assertEqual(self.stats()[1],10)
        self.inject.send(wire(tagged(frame()),41));self.poll();self.assertEqual(self.peer.recv(4096),tagged(frame()))
    def test_native_rendezvous_matches_existing_ipv4_ipv6_vlan_and_ethernet_hash(self):
        gates=Gates([11,19]);gates.apply({p:dict(collect=True,distribute=True) for p in gates.state})
        for i in range(32):
            ipv4=bytes(12)+b'\x08\0\x45'+bytes(8)+b'\x11'+bytes(2)+bytes([192,0,2,i,198,51,100,2])+bytes(26)
            ipv6=bytes(12)+b'\x86\xdd\x60'+bytes(7)+bytes([i])*32+bytes(6)
            for value in (frame(tail=i),ipv4,ipv6,tagged(ipv4),tagged(ipv6)):
                selected=gates.transmit(value,lambda *_:None)
                self.peer.send(value);self.poll()
                self.assertEqual(self.collect.recv(4096),encoded(value,41 if selected==11 else 42))
        self.gates(collect=2,distribute=2);self.peer.send(frame());self.poll()
        self.assertEqual(self.collect.recv(4096),encoded(frame(),42))
    def test_gate_withdrawal_and_expiration_without_python_poll(self):
        self.gates(ttl=.12)
        cpus=sorted(os.sched_getaffinity(0));checked(self.lib.ffn_packet_start(self.ctx,cpus[0],cpus[-1]));checked(self.lib.ffn_packet_resume(self.ctx))
        self.peer.settimeout(1);self.inject.send(wire(frame(),41));self.assertEqual(self.peer.recv(4096),frame());self.peer.setblocking(False)
        time.sleep(.15)
        self.inject.send(wire(frame(),41));self.peer.send(frame());time.sleep(.04)
        checked(self.lib.ffn_packet_pause(self.ctx));self.empty(self.peer);self.empty(self.collect)
        self.assertEqual(self.stats()[0],2)
        self.gates(collect=0,distribute=0);checked(self.lib.ffn_packet_resume(self.ctx))
        self.inject.send(wire(frame(),41));time.sleep(.04);checked(self.lib.ffn_packet_pause(self.ctx));self.empty(self.peer)
    def test_hardware_egress_ack_expiry_falls_back_to_software_member(self):
        self.gates(offload=.1);value=tagged(frame())
        self.peer.send(value);self.poll();self.assertEqual(self.collect.recv(4096),encoded(value,0x8001))
        time.sleep(.12);self.peer.send(value);self.poll()
        self.assertIn(self.collect.recv(4096),(encoded(value,41),encoded(value,42)))
        self.assertEqual(self.stats()[3],1)
        out=(C.c_uint64*4)();checked(self.lib.ffn_aggregate_unit_stats(self.ctx,123,out,4));self.assertEqual(out[2],2)
    def test_normalized_inspection_and_local_management_bypass(self):
        self.gates(inspect=1)
        value=tagged(frame(tail=2));self.inject.send(wire(value,41));self.inject.send(wire(value,42));self.poll()
        self.assertEqual(self.peer.recv(4096),value);self.empty(self.peer)
        local=bytes(12)+b'\x08\0\x45'+bytes(15)+bytes([192,0,2,1])+bytes(25)+b'\2'
        self.inject.send(wire(tagged(local),41));self.poll();self.assertEqual(self.peer.recv(4096),tagged(local))
        self.network(tag=124);self.inject.send(wire(tagged(local),41));self.poll();self.empty(self.peer)
        self.assertEqual(self.stats()[7],1)
    def test_configuration_barrier_and_network_transition_drop(self):
        cpus=sorted(os.sched_getaffinity(0));checked(self.lib.ffn_packet_start(self.ctx,cpus[0],cpus[-1]));checked(self.lib.ffn_packet_resume(self.ctx))
        with self.assertRaises(OSError):self.network()
        checked(self.lib.ffn_packet_pause(self.ctx));self.gates(ready=False);checked(self.lib.ffn_packet_resume(self.ctx))
        self.inject.send(wire(frame(),41));self.peer.send(frame());time.sleep(.04);checked(self.lib.ffn_packet_pause(self.ctx))
        self.empty(self.peer);self.empty(self.collect);self.assertEqual(self.stats()[2],2)
    def test_invalid_native_mapping_and_configuration_are_rejected(self):
        members=(Member*2)(Member(11,41,0x8001),Member(19,42,41))
        self.assertFalse(self.lib.ffn_aggregate_adopt(self.rx.fileno(),self.tx.fileno(),self.tap.fileno(),members,2,1))
        self.assertEqual(self.lib.ffn_aggregate_gates(self.ctx,0,3,0,999,0,1,None,None),-1)
        bad=(Unit*1)(Unit(tag=4095,mtu=1500))
        self.assertEqual(self.lib.ffn_aggregate_network(self.ctx,bad,1),-1)
        self.inject.send(wire(frame(),41));self.poll();self.assertEqual(self.peer.recv(4096),frame())
    def test_control_unknown_source_and_oversized_frames_never_delivered(self):
        for value in (frame(b'\x88\x09'),frame(b'\x88\xcc')):self.inject.send(wire(value,41))
        self.inject.send(wire(frame(),43));self.inject.send(wire(bytes(1519),41));self.peer.send(bytes(1519));self.poll()
        self.empty(self.peer);self.empty(self.collect);self.assertEqual(self.stats()[6],2)

    def test_control_wrapper_reconfigures_live_workers_without_replacing_tap(self):
        owner=AggregateOwner.__new__(AggregateOwner)
        owner.lib=self.lib;owner.handle=self.ctx;owner.parent='ae1';owner.members=[11,19]
        owner.port=11;owner.started=False;owner.network_key=None;owner.unit_names={}
        gates=Gates(owner.members);gates.apply({p:dict(collect=True,distribute=True) for p in owner.members})
        now=time.monotonic();engine=SimpleNamespace(members={p:dict(lease=now+5,deadline=now+5) for p in owner.members})
        inspector=SimpleNamespace(handle=None,lib=None,cfg={'ports':[]},counts=Counter())
        network=dict(enabled=False,mtu=1500,units=[dict(name='ae1.7',tag=123,mtu=1500,addresses=['192.0.2.1/24'])])
        owner.configure(network,set(),gates,engine,None,True,inspector,now);owner.resume()
        workers=owner.workers();self.peer.settimeout(1)
        self.inject.send(wire(tagged(frame()),41));self.assertEqual(self.peer.recv(4096),tagged(frame()))
        owner.pause();counts,unit_counts=owner.snapshot(gates,None,inspector)
        self.assertEqual(gates.rx[11],1);self.assertEqual(unit_counts['ae1.7']['rx_packets'],1)
        network['units'][0]['tag']=124
        owner.configure(network,set(),gates,engine,None,True,inspector,time.monotonic());owner.resume()
        self.inject.send(wire(tagged(frame()),41));self.inject.send(wire(tagged(frame(),124),41))
        self.assertEqual(self.peer.recv(4096),tagged(frame(),124));owner.pause()
        self.assertEqual(owner.workers()['rx_tid'],workers['rx_tid'])
        self.assertEqual(owner.workers()['tx_tid'],workers['tx_tid'])
        counts,_=owner.snapshot(gates,None,inspector);self.assertEqual(counts['unconfigured_or_invalid_vlan_drop'],1)


if __name__=='__main__':unittest.main()
