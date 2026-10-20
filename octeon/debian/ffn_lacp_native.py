# SPDX-License-Identifier: GPL-2.0-or-later
"""The native LACP engine (octeon/native/ffn_lacp.c) behind the Python engine's API.

Conformance shim only: it lets test_lacp_engine.py and test-lacp-engine-linux.py
drive the C engine with FFN_LACP_ENGINE=native. Production owners link the
engine directly (ffn-aggregated); nothing on the appliance imports this.
"""
from collections.abc import Mapping
import ctypes as C
import math
import os
from ffn_lacp_engine import mac_bytes,ZERO,ACTIVITY,TIMEOUT,AGGREGATION,SYNC,COLLECTING,DISTRIBUTING,DEFAULTED,EXPIRED,IDENTITY
from ffn_lacp_packets import FLAGS

LIBRARY=os.environ.get('FFN_LACP_LIB','/usr/local/lib/libffn-lacp.so')
PDU=124
MEMBERS=8
RX=('disabled','current','expired','defaulted')


class Info(C.Structure):
    _fields_=[('system_priority',C.c_uint16),('system',C.c_uint8*6),('key',C.c_uint16),
              ('port_priority',C.c_uint16),('port',C.c_uint16),('state',C.c_uint8)]


class Pdu(C.Structure):
    _fields_=[('source',C.c_uint8*6),('actor',Info),('partner',Info),('collector_max_delay',C.c_uint16)]


class Gate(C.Structure):
    _fields_=[('collect',C.c_bool),('distribute',C.c_bool)]


class MemberConfig(C.Structure):
    _fields_=[('port',C.c_uint16),('source',C.c_uint8*6)]


class Config(C.Structure):
    _fields_=[('system',C.c_uint8*6),('key',C.c_uint16),('system_priority',C.c_uint16),('active',C.c_bool),
              ('fast',C.c_bool),('min_links',C.c_uint),('collecting',C.c_bool),('count',C.c_uint),
              ('members',MemberConfig*MEMBERS)]


class Transmit(C.Structure):
    _fields_=[('port',C.c_uint16),('frame',C.c_uint8*PDU)]


class MemberStatus(C.Structure):
    _fields_=[('port',C.c_uint16),('rx',C.c_int),('link',C.c_bool),('speed_mbps',C.c_uint32),('lease_us',C.c_uint64),
              ('deadline_us',C.c_uint64),('selected',C.c_bool),('matched',C.c_bool),('sync',C.c_bool),('dirty',C.c_bool),
              ('has_peer',C.c_bool),('peer',Info),('tx_attempts',C.c_uint64),('received',C.c_uint64),('invalid',C.c_uint64),
              ('has_gate',C.c_bool),('gate',Gate)]


APPLY=C.CFUNCTYPE(C.c_int,C.c_void_p,C.POINTER(C.c_uint16),C.POINTER(Gate),C.c_uint,C.POINTER(C.c_char),C.c_size_t)

_lib=None


def library(path=LIBRARY):
    global _lib
    if _lib is not None:return _lib
    lib=C.CDLL(path,use_errno=True)
    lib.ffn_lacp_decode.argtypes=[C.c_char_p,C.c_size_t,C.POINTER(Pdu)];lib.ffn_lacp_decode.restype=C.c_int
    lib.ffn_lacp_encode.argtypes=[C.POINTER(Info),C.POINTER(Info),C.c_char_p,C.c_char_p];lib.ffn_lacp_encode.restype=C.c_int
    lib.ffn_lacp_create.argtypes=[C.POINTER(Config),APPLY,C.c_void_p,C.c_char_p,C.c_size_t];lib.ffn_lacp_create.restype=C.c_void_p
    lib.ffn_lacp_destroy.argtypes=[C.c_void_p];lib.ffn_lacp_destroy.restype=None
    lib.ffn_lacp_link.argtypes=[C.c_void_p,C.c_uint16,C.c_bool,C.c_uint32,C.c_uint64,C.c_uint64];lib.ffn_lacp_link.restype=C.c_int
    lib.ffn_lacp_receive.argtypes=[C.c_void_p,C.c_uint16,C.c_char_p,C.c_size_t,C.c_uint64];lib.ffn_lacp_receive.restype=C.c_int
    lib.ffn_lacp_tick.argtypes=[C.c_void_p,C.c_uint64];lib.ffn_lacp_tick.restype=C.c_int
    lib.ffn_lacp_transmissions.argtypes=[C.c_void_p,C.c_uint64,C.POINTER(Transmit),C.c_uint];lib.ffn_lacp_transmissions.restype=C.c_int
    lib.ffn_lacp_stop.argtypes=[C.c_void_p,C.c_uint64];lib.ffn_lacp_stop.restype=C.c_int
    lib.ffn_lacp_abort.argtypes=[C.c_void_p,C.c_char_p,C.c_uint64];lib.ffn_lacp_abort.restype=C.c_int
    lib.ffn_lacp_fault.argtypes=[C.c_void_p];lib.ffn_lacp_fault.restype=C.c_char_p
    lib.ffn_lacp_gates_verified.argtypes=[C.c_void_p];lib.ffn_lacp_gates_verified.restype=C.c_bool
    lib.ffn_lacp_count.argtypes=[C.c_void_p];lib.ffn_lacp_count.restype=C.c_uint
    lib.ffn_lacp_port_at.argtypes=[C.c_void_p,C.c_uint];lib.ffn_lacp_port_at.restype=C.c_int
    lib.ffn_lacp_actor.argtypes=[C.c_void_p,C.c_uint16,C.POINTER(Info)];lib.ffn_lacp_actor.restype=C.c_int
    lib.ffn_lacp_distributing.argtypes=[C.c_void_p,C.POINTER(C.c_uint16),C.c_uint];lib.ffn_lacp_distributing.restype=C.c_uint
    lib.ffn_lacp_applied.argtypes=[C.c_void_p,C.POINTER(Gate),C.c_uint];lib.ffn_lacp_applied.restype=C.c_int
    lib.ffn_lacp_member.argtypes=[C.c_void_p,C.c_uint16,C.POINTER(MemberStatus)];lib.ffn_lacp_member.restype=C.c_int
    lib.ffn_lacp_mark_dirty.argtypes=[C.c_void_p,C.c_uint16];lib.ffn_lacp_mark_dirty.restype=C.c_int
    lib.ffn_lacp_now.argtypes=[C.c_void_p];lib.ffn_lacp_now.restype=C.c_uint64
    _lib=lib
    return lib


def mac(raw):return ':'.join('%02x'%b for b in raw)


def info_from(value):
    out=Info();out.system_priority=value['system_priority'];out.system[:]=mac_bytes(value['system'])
    out.key=value['key'];out.port_priority=value['port_priority'];out.port=value['port'];out.state=value['state']
    return out


def info_to(value,flags=False):
    result=dict(system_priority=value.system_priority,system=mac(bytes(value.system)),key=value.key,
                port_priority=value.port_priority,port=value.port,state=value.state)
    if flags:result['flags']={name:bool(value.state&(1<<i)) for i,name in enumerate(FLAGS)}
    return result


def microseconds(now):
    if not isinstance(now,(float,int)) or not math.isfinite(now) or now<0:raise ValueError('Invalid monotonic time')
    return int(round(now*1000000))


def encode(actor,partner,source):
    raw=mac_bytes(source)
    if raw[0]&1 or raw==bytes(6):raise ValueError('Unicast source MAC required')
    out=C.create_string_buffer(PDU)
    if library().ffn_lacp_encode(C.byref(info_from(actor)),C.byref(info_from(partner)),raw,out):raise ValueError('Unicast source MAC required')
    return out.raw


def decode(frame):
    if not isinstance(frame,bytes):raise ValueError('truncated LACP frame')
    pdu=Pdu()
    if library().ffn_lacp_decode(frame,len(frame),C.byref(pdu)):raise ValueError('invalid LACP frame')
    return dict(source=mac(bytes(pdu.source)),actor=info_to(pdu.actor,True),partner=info_to(pdu.partner,True),
                collector_max_delay=pdu.collector_max_delay)


class MemberView:
    """Live read view of one member, with the one write the tests use (dirty)."""
    def __init__(self,engine,port):self.engine=engine;self.port=port
    def _status(self):
        out=MemberStatus()
        if self.engine.lib.ffn_lacp_member(self.engine.handle,self.port,C.byref(out)):raise KeyError(self.port)
        return out
    def __getitem__(self,key):
        s=self._status()
        if key=='source':return self.engine.sources[self.port]
        if key=='link':return bool(s.link)
        if key=='speed':return s.speed_mbps
        if key=='lease':return s.lease_us/1000000
        if key=='deadline':return s.deadline_us/1000000
        if key=='peer':return info_to(s.peer,True) if s.has_peer else None
        if key=='rx':return RX[s.rx]
        if key=='selected':return bool(s.selected)
        if key=='matched':return bool(s.matched)
        if key=='sync':return bool(s.sync)
        if key=='dirty':return bool(s.dirty)
        if key=='tx':return s.tx_attempts
        if key=='rx_count':return s.received
        if key=='invalid':return s.invalid
        raise KeyError(key)
    def __setitem__(self,key,value):
        if key!='dirty' or value is not True:raise TypeError('native member state is read only except dirty=True')
        if self.engine.lib.ffn_lacp_mark_dirty(self.engine.handle,self.port):raise KeyError(self.port)
    def get(self,key,default=None):
        try:return self[key]
        except KeyError:return default


class Members(Mapping):
    def __init__(self,engine):self.engine=engine
    def _ports(self):
        lib=self.engine.lib;ports=[]
        for i in range(lib.ffn_lacp_count(self.engine.handle)):ports.append(lib.ffn_lacp_port_at(self.engine.handle,i))
        return ports
    def __iter__(self):return iter(self._ports())
    def __len__(self):return len(self._ports())
    def __contains__(self,port):return port in self._ports()
    def __getitem__(self,port):
        if port not in self._ports():raise KeyError(port)
        return MemberView(self.engine,port)


class Engine:
    def __init__(self,system,key,members,driver,*,activity='active',rate='fast',system_priority=32768,min_links=1,collecting=True):
        raw=mac_bytes(system)
        if raw==bytes(6) or raw[0]&1:raise ValueError('Unicast system MAC required')
        if type(key) is not int or not 1<=key<=65535:raise ValueError('Invalid actor key')
        if type(system_priority) is not int or not 1<=system_priority<=65535:raise ValueError('Invalid system priority')
        if (not isinstance(members,dict) or not 1<=len(members)<=8 or
            any(type(p) is not int or not 1<=p<=65535 for p in members)):raise ValueError('Invalid members')
        if type(min_links) is not int or not 1<=min_links<=len(members):raise ValueError('Invalid minimum links')
        if activity not in ('active','passive') or rate not in ('fast','slow'):raise ValueError('Invalid LACP mode')
        if type(collecting) is not bool:raise ValueError('Invalid collection permission')
        self.lib=library();self.driver=driver;self.handle=None
        self.collecting=collecting;self.system=system.lower();self.key=key;self.priority=system_priority
        self.active=activity=='active';self.fast=rate=='fast';self.min_links=min_links;self.sources={}
        cfg=Config();cfg.system[:]=raw;cfg.key=key;cfg.system_priority=system_priority;cfg.active=self.active
        cfg.fast=self.fast;cfg.min_links=min_links;cfg.collecting=collecting;cfg.count=len(members)
        for i,(port,source) in enumerate(members.items()):
            value=mac_bytes(source)
            if value[0]&1 or value==bytes(6):raise ValueError('Unicast member MAC required')
            cfg.members[i].port=port;cfg.members[i].source[:]=value;self.sources[port]=source
        self._callback=APPLY(self._apply)
        err=C.create_string_buffer(256)
        self.handle=self.lib.ffn_lacp_create(C.byref(cfg),self._callback,None,err,len(err))
        if not self.handle:raise ValueError(err.value.decode())
        self.members=Members(self)

    def __del__(self):
        if getattr(self,'handle',None):self.lib.ffn_lacp_destroy(self.handle);self.handle=None

    def _apply(self,ctx,ports,gates,count,err,errsz):
        desired={ports[i]:dict(collect=bool(gates[i].collect),distribute=bool(gates[i].distribute)) for i in range(count)}
        try:result=self.driver.apply(desired)
        except Exception as error:
            message=(str(error) or type(error).__name__).encode()[:errsz-1]
            C.memmove(err,message+b'\0',len(message)+1)
            return -1
        # Any readback that is not exactly the desired map must reach the engine
        # as a mismatch, so a malformed result reads back as the complement.
        try:
            if set(result)!=set(desired) or any(set(v)!={'collect','distribute'} or any(type(x) is not bool for x in v.values()) for v in result.values()):raise ValueError
            for i in range(count):gates[i].collect=result[ports[i]]['collect'];gates[i].distribute=result[ports[i]]['distribute']
        except (TypeError,ValueError,KeyError,AttributeError):
            for i in range(count):gates[i].collect=not gates[i].collect
        return 0

    @property
    def fault(self):
        value=self.lib.ffn_lacp_fault(self.handle)
        return value.decode() if value is not None else None

    @property
    def applied(self):
        count=self.lib.ffn_lacp_count(self.handle);gates=(Gate*count)()
        if self.lib.ffn_lacp_applied(self.handle,gates,count):return None
        return {port:dict(collect=bool(gates[i].collect),distribute=bool(gates[i].distribute)) for i,port in enumerate(self.members)}

    @property
    def now(self):
        value=self.lib.ffn_lacp_now(self.handle)
        return value/1000000 if value else None

    def closed(self):return {p:{'collect':False,'distribute':False} for p in self.members}

    def _time(self,now):
        stamp=microseconds(now)
        current=self.lib.ffn_lacp_now(self.handle)
        if current and stamp<current:raise ValueError('Monotonic clock moved backward')
        return stamp

    def actor(self,port):
        out=Info()
        if self.lib.ffn_lacp_actor(self.handle,port,C.byref(out)):raise KeyError(port)
        return info_to(out)

    def link(self,port,up,speed,now,lease_seconds=3):
        stamp=self._time(now)
        if type(up) is not bool or type(speed) is not int or speed<0 or up and speed==0:raise ValueError('Invalid carrier/speed')
        if not isinstance(lease_seconds,(int,float)) or not 0<lease_seconds<=30:raise ValueError('Carrier lease must be 0..30 seconds')
        if port not in self.members:raise KeyError(port)
        if self.lib.ffn_lacp_link(self.handle,port,up,speed,stamp,int(round(lease_seconds*1000000))):raise ValueError('Invalid carrier/speed')

    def receive(self,port,frame,now):
        stamp=self._time(now)
        if port not in self.members:raise KeyError(port)
        if not isinstance(frame,bytes):frame=b''
        result=self.lib.ffn_lacp_receive(self.handle,port,frame,len(frame),stamp)
        if result<0:raise ValueError('Invalid receive')
        return bool(result)

    def tick(self,now):
        if self.lib.ffn_lacp_tick(self.handle,self._time(now)):raise ValueError('Invalid tick')

    def transmissions(self,now):
        out=(Transmit*MEMBERS)()
        count=self.lib.ffn_lacp_transmissions(self.handle,self._time(now),out,MEMBERS)
        if count<0:raise ValueError('Invalid transmission request')
        return [(out[i].port,bytes(out[i].frame)) for i in range(count)]

    def stop(self,now):
        if self.lib.ffn_lacp_stop(self.handle,self._time(now)):raise ValueError('Invalid stop')

    def abort(self,reason,now):
        if self.lib.ffn_lacp_abort(self.handle,str(reason).encode(),self._time(now)):raise ValueError('Invalid abort')

    def status(self,now):
        self.tick(now)
        ports=(C.c_uint16*MEMBERS)();count=self.lib.ffn_lacp_distributing(self.handle,ports,MEMBERS)
        applied=self.applied
        return dict(fault=self.fault,gates_verified=applied is not None,distributing=[ports[i] for i in range(count)],
            members={p:dict(rx=m['rx'],selected=m['selected'],matched=m['matched'],actor=self.actor(p),peer=m['peer'],
                tx_attempts=m['tx'],received=m['rx_count'],invalid=m['invalid'],gates=(applied or {}).get(p))
                for p,m in self.members.items()})
