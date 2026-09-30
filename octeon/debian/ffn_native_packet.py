#!/usr/bin/env python3
"""Control-only ABI for the C packet owner; no packet bytes cross Python."""
import ctypes as C
import ipaddress
import os
from ffn_packet_cpus import CpuReservations

LIBRARY='/usr/local/lib/libffn-packet.so'
COUNTERS=('rx','tx','envelope_rejected','length_drop','backpressure_drop',
          'admin_down_drop','local_input','inspection_drop','bypassed',
          'unsupported_pass','malformed_pass','no_match','alert','block',
          'outgoing_ignored','invalid_verdict')


def library(path=LIBRARY):
    lib=C.CDLL(path,use_errno=True)
    lib.ffn_packet_abi.restype=C.c_uint
    if lib.ffn_packet_abi()!=2:raise RuntimeError('Unsupported native packet ABI')
    lib.ffn_packet_open.argtypes=[C.c_char_p,C.c_char_p,C.c_char_p,C.c_uint]
    lib.ffn_packet_open.restype=C.c_void_p
    lib.ffn_packet_adopt.argtypes=[C.c_int,C.c_int,C.c_int,C.c_uint]
    lib.ffn_packet_adopt.restype=C.c_void_p
    lib.ffn_packet_configure.argtypes=[C.c_void_p,C.c_char_p,C.c_uint,C.c_char_p,C.c_uint,C.c_void_p,C.c_void_p]
    lib.ffn_packet_configure.restype=C.c_int
    lib.ffn_packet_poll.argtypes=[C.c_void_p,C.c_uint]
    lib.ffn_packet_poll.restype=C.c_int
    lib.ffn_packet_start.argtypes=[C.c_void_p,C.c_int,C.c_int]
    lib.ffn_packet_start.restype=C.c_int
    for name in ('ffn_packet_pause','ffn_packet_resume'):
        getattr(lib,name).argtypes=[C.c_void_p];getattr(lib,name).restype=C.c_int
    lib.ffn_packet_workers.argtypes=[C.c_void_p,C.POINTER(C.c_int),C.c_uint]
    lib.ffn_packet_workers.restype=C.c_int
    lib.ffn_packet_counters.argtypes=[C.c_void_p,C.POINTER(C.c_uint64),C.c_uint]
    lib.ffn_packet_counters.restype=C.c_int
    lib.ffn_packet_receive_stats.argtypes=[C.c_void_p,C.POINTER(C.c_uint64),C.c_uint]
    lib.ffn_packet_receive_stats.restype=C.c_int
    # Keep control tools compatible with a previously loaded packet library.
    if hasattr(lib,'ffn_packet_transmit_stats'):
        lib.ffn_packet_transmit_stats.argtypes=[C.c_void_p,C.POINTER(C.c_uint64),C.c_uint]
        lib.ffn_packet_transmit_stats.restype=C.c_int
    lib.ffn_packet_close.argtypes=[C.c_void_p]
    lib.ffn_packet_close.restype=None
    return lib


def checked(result):
    if result<0:
        error=C.get_errno()
        raise OSError(error,os.strerror(error))
    return result


class PacketOwner:
    def __init__(self,port,source,lib=None):
        if type(port) is not int or not 1<=port<=24 or type(source) is not int or not 0<=source<=65535:
            raise ValueError('Invalid commissioned port mapping')
        self.lib=lib or library();self.port=port;self.started=False
        self.handle=self.lib.ffn_packet_open(b'ffnpkt0',b'ffn-data',('p'+str(port)).encode(),source)
        if not self.handle:checked(-1)

    def configure(self,addresses,inspector):
        values=[ipaddress.ip_interface(a).ip for a in addresses]
        v4=[a.packed for a in values if a.version==4];v6=[a.packed for a in values if a.version==6]
        if len(v4)>64 or len(v6)>64:raise ValueError('Native local address limit exceeded')
        active=bool(inspector.handle and self.port in inspector.cfg['ports'])
        checked(self.lib.ffn_packet_configure(self.handle,b''.join(v4),len(v4),b''.join(v6),len(v6),
            inspector.handle if active else None,
            C.cast(inspector.lib.ffn_inline_scan,C.c_void_p) if active else None))

    def poll(self):
        checked(self.lib.ffn_packet_poll(self.handle,100))

    def pause(self):
        checked(self.lib.ffn_packet_pause(self.handle))

    def resume(self):
        if not self.started:
            self.cpu_reservations=CpuReservations()
            self.cpu_token,cpus=self.cpu_reservations.reserve()
            try:checked(self.lib.ffn_packet_start(self.handle,*cpus))
            except BaseException:
                # start may have created one worker; join it before releasing CPUs.
                self.close()
                raise
            self.started=True
        checked(self.lib.ffn_packet_resume(self.handle))

    def workers(self):
        out=(C.c_int*8)();checked(self.lib.ffn_packet_workers(self.handle,out,8))
        batches=(C.c_uint64*3)();checked(self.lib.ffn_packet_receive_stats(self.handle,batches,3))
        tx={}
        if hasattr(self.lib,'ffn_packet_transmit_stats'):
            sent=(C.c_uint64*3)();checked(self.lib.ffn_packet_transmit_stats(self.handle,sent,3))
            tx=dict(transmit_syscalls=sent[0],transmit_frames=sent[1],transmit_max_batch=sent[2])
        return dict(count=out[0],rx_cpu=out[1],tx_cpu=out[2],rx_tid=out[3],tx_tid=out[4],
                    paused=bool(out[5]),stopped=bool(out[6]),error=out[7],
                    scheduling='ordered-rx-tx',flow_parallelism=False,
                    cpu_allocation='shared-reservations',receive_syscalls=batches[0],
                    receive_frames=batches[1],receive_max_batch=batches[2],**tx)

    def snapshot(self,inspector):
        values=(C.c_uint64*len(COUNTERS))()
        checked(self.lib.ffn_packet_counters(self.handle,values,len(values)))
        result=dict(zip(COUNTERS,values))
        result['rx_p'+str(self.port)]=result.pop('rx')
        result['tx_p'+str(self.port)]=result.pop('tx')
        for name in ('bypassed','unsupported_pass','malformed_pass','no_match','alert','block'):
            inspector.counts[name]=result[name]
            if name!='bypassed':inspector.counts['port_%d_%s'%(self.port,name)]=result[name]
        return result

    def close(self):
        if self.handle:self.lib.ffn_packet_close(self.handle);self.handle=None
        if getattr(self,'cpu_token',None):
            self.cpu_reservations.release(self.cpu_token);self.cpu_token=None
