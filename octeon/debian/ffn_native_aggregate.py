#!/usr/bin/env python3
"""Control ABI for native aggregate forwarding. No data packets enter Python."""
import ctypes as C
import ipaddress
import socket
from ffn_native_packet import PacketOwner, library, checked, COUNTERS


class Member(C.Structure):
    _fields_=[('front',C.c_uint32),('source',C.c_uint32),('alias',C.c_uint32)]


class Unit(C.Structure):
    _fields_=[('tag',C.c_uint32),('mtu',C.c_uint32),('n4',C.c_uint32),('n6',C.c_uint32),
              ('local4',(C.c_uint8*4)*32),('local6',(C.c_uint8*16)*32)]


def aggregate_library(path=None):
    lib=library(path) if path else library()
    lib.ffn_aggregate_abi.restype=C.c_uint
    if lib.ffn_aggregate_abi()!=1:raise RuntimeError('Unsupported native aggregate ABI')
    args=[C.POINTER(Member),C.c_uint,C.c_uint]
    lib.ffn_aggregate_open.argtypes=[C.c_char_p,C.c_char_p,C.c_char_p]+args
    lib.ffn_aggregate_open.restype=C.c_void_p
    lib.ffn_aggregate_adopt.argtypes=[C.c_int,C.c_int,C.c_int]+args
    lib.ffn_aggregate_adopt.restype=C.c_void_p
    lib.ffn_aggregate_control_open.argtypes=[C.c_char_p,C.POINTER(C.c_uint32),C.c_uint]
    lib.ffn_aggregate_control_open.restype=C.c_int
    lib.ffn_aggregate_network.argtypes=[C.c_void_p,C.POINTER(Unit),C.c_uint]
    lib.ffn_aggregate_network.restype=C.c_int
    lib.ffn_aggregate_gates.argtypes=[C.c_void_p,C.c_uint,C.c_uint,C.c_uint,C.c_uint64,C.c_uint64,C.c_uint,C.c_void_p,C.c_void_p]
    lib.ffn_aggregate_gates.restype=C.c_int
    lib.ffn_aggregate_stats.argtypes=[C.c_void_p,C.POINTER(C.c_uint64),C.c_uint]
    lib.ffn_aggregate_stats.restype=C.c_int
    for name in ('member','unit'):
        fn=getattr(lib,'ffn_aggregate_'+name+'_stats')
        fn.argtypes=[C.c_void_p,C.c_uint,C.POINTER(C.c_uint64),C.c_uint];fn.restype=C.c_int
    return lib


def control_socket(sources,lib=None):
    lib=lib or aggregate_library()
    values=(C.c_uint32*len(sources))(*sources)
    fd=lib.ffn_aggregate_control_open(b'ffnpkt0',values,len(values));checked(fd)
    sock=socket.socket(fileno=fd);sock.setblocking(False);return sock


def units(parent,network,local):
    values=list(network.get('units',[]))
    if network.get('enabled',True):
        values=[dict(name=parent,tag=4096,mtu=network['mtu'],addresses=list(local))]+values
    result=(Unit*len(values))()
    names={}
    for row,value in zip(result,values):
        row.tag=value['tag'];row.mtu=value['mtu'];names[row.tag]=value['name']
        addresses=[ipaddress.ip_interface(a).ip.packed if isinstance(a,str) else a for a in value['addresses']]
        for size,field,count in ((4,'local4','n4'),(16,'local6','n6')):
            packed=[a for a in addresses if len(a)==size]
            if len(packed)>32:raise ValueError('Native aggregate address limit exceeded')
            setattr(row,count,len(packed))
            for dst,src in zip(getattr(row,field),packed):dst[:]=src
    return result,names


class AggregateOwner(PacketOwner):
    def __init__(self,parent,mapping,aliases=None,lib=None):
        self.lib=lib or aggregate_library();self.parent=parent;self.members=sorted(mapping)
        self.port=self.members[0];self.started=False;self.network_key=None;self.unit_names={}
        aliases=aliases or {}
        members=(Member*len(mapping))(*(Member(p,mapping[p],aliases.get(p,0)) for p in self.members))
        self.handle=self.lib.ffn_aggregate_open(b'ffnpkt0',b'ffn-data',parent.encode(),members,len(members),int(parent[2:]))
        if not self.handle:checked(-1)

    def configure(self,network,local,gates,engine,offload,ready,inspector,now):
        rows,names=units(self.parent,network,local)
        key=bytes(rows)
        if key!=self.network_key:
            checked(self.lib.ffn_aggregate_network(self.handle,rows,len(rows)))
            self.network_key=key;self.unit_names=names
        collect=distribute=inspect=0;deadlines=[]
        for i,port in enumerate(self.members):
            state=gates.state[port];member=engine.members[port]
            if state['collect']:collect|=1<<i
            if state['distribute']:distribute|=1<<i
            if state['collect'] or state['distribute']:deadlines.append(min(member['lease'],member['deadline']))
            if inspector.handle and port in inspector.cfg['ports']:inspect|=1<<i
        deadline=int(min(deadlines)*1000) if deadlines else 0
        offload_deadline=int(offload.deadline*1000) if offload and offload.ready(gates,now) else 0
        checked(self.lib.ffn_aggregate_gates(self.handle,collect,distribute,inspect,deadline,offload_deadline,int(ready),
            inspector.handle if inspect else None,C.cast(inspector.lib.ffn_inline_scan,C.c_void_p) if inspect else None))

    def snapshot(self,gates,offload,inspector):
        values=(C.c_uint64*len(COUNTERS))();checked(self.lib.ffn_packet_counters(self.handle,values,len(values)))
        counts=dict(zip(COUNTERS,values))
        verdicts=('bypassed','unsupported_pass','malformed_pass','no_match','alert','block')
        for name in verdicts:inspector.counts[name]=counts[name]
        stats=(C.c_uint64*8)();checked(self.lib.ffn_aggregate_stats(self.handle,stats,8))
        names=('gate_drops','unconfigured_or_invalid_vlan_drop','network_update_drop','offload_tx',
               'rx_queue_drop','tx_queue_drop','control_frame_ignored','inspection_drop')
        counts.update(zip(names,stats));gates.dropped=counts['gate_drops']
        if offload:offload.transmitted=counts['offload_tx']
        for i,port in enumerate(self.members):
            checked(self.lib.ffn_aggregate_member_stats(self.handle,i,stats,8))
            gates.rx[port],gates.tx[port]=stats[0],stats[1]
            for name,value in zip(verdicts,stats[2:]):inspector.counts['port_%d_%s'%(port,name)]=value
        unit_counts={}
        for tag,name in self.unit_names.items():
            values=(C.c_uint64*4)();checked(self.lib.ffn_aggregate_unit_stats(self.handle,tag,values,4))
            unit_counts[name]=dict(zip(('rx_packets','rx_bytes','tx_packets','tx_bytes'),values))
        return counts,unit_counts
