#!/usr/bin/env python3
"""Real AF_PACKET/TUN verification inside private mount/network namespaces."""
import ctypes as C
import os
from pathlib import Path
import socket
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'debian'))
from ffn_native_packet import library,checked


def command(*args):subprocess.run(args,check=True)


def inside():
    Path('/run/netns').mkdir(exist_ok=True)
    command('mount','-t','tmpfs','tmpfs','/run/netns')
    command('ip','netns','add','ffnlab')
    command('ip','netns','exec','ffnlab','ip','tuntap','add','lab0','mode','tap')
    command('ip','netns','exec','ffnlab','ip','link','set','lab0','up')
    command('ip','link','add','testa','type','veth','peer','name','testb')
    for name in ('testa','testb'):command('ip','link','set',name,'up')
    lib=library(str(Path(__file__).with_name('libffn-packet.so')))
    ctx=lib.ffn_packet_open(b'testa',b'ffnlab',b'lab0',28)
    if not ctx:checked(-1)
    original=os.open('/proc/self/ns/net',os.O_RDONLY)
    target=os.open('/run/netns/ffnlab',os.O_RDONLY)
    libc=C.CDLL(None,use_errno=True);libc.setns.argtypes=[C.c_int,C.c_int]
    checked(libc.setns(target,0))
    capture=socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(0x88b5));capture.bind(('lab0',0))
    checked(libc.setns(original,0));os.close(original);os.close(target)
    wire=socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3));wire.bind(('testb',0))
    for sock in (wire,capture):
        sock.setsockopt(263,23,1);sock.settimeout(1)
    frame=b'\xff'*6+b'\x02\0\0\0\0\1'+b'\x88\xb5'+bytes(range(46))
    try:
        wire.send(b'\0\x18\0\x1d'+frame)
        wire.send(b'\0\x18\0\x1c'+frame)
        checked(lib.ffn_packet_poll(ctx,20))
        assert capture.recv(4096)==frame
        capture.send(frame)
        checked(lib.ffn_packet_poll(ctx,20))
        expected=b'\1\0\x1c\0'+frame[:12]+bytes(8)+frame[12:]
        for _ in range(32):
            packet=wire.recv(4096)
            if packet==expected:break
        else:raise AssertionError('Native wire transmit not received')
        counts=(C.c_uint64*16)();checked(lib.ffn_packet_counters(ctx,counts,16))
        assert counts[0]==1 and counts[1]>=1 and counts[2]==0, list(counts)
        command('ip','netns','exec','ffnlab','ip','link','set','lab0','down')
        checked(lib.ffn_packet_poll(ctx,20))
        command('ip','netns','exec','ffnlab','ip','link','set','lab0','up')
        # The separate observer AF_PACKET socket retains the link-down error.
        # Clear that notification before checking newly delivered traffic.
        capture.getsockopt(socket.SOL_SOCKET,socket.SO_ERROR)
        wire.send(b'\0\x18\0\x1c'+frame)
        checked(lib.ffn_packet_poll(ctx,20))
        assert capture.recv(4096)==frame
        print('PASS native socket creation/filter, namespace restoration, TAP ioctl, bidirectional wire framing; '+os.uname().machine)
    finally:
        wire.close();capture.close();lib.ffn_packet_close(ctx)


if __name__=='__main__':
    if sys.argv[1:]==['--inside']:inside()
    else:command('unshare','--mount','--net','--propagation','private',sys.executable,str(Path(__file__).resolve()),'--inside')
