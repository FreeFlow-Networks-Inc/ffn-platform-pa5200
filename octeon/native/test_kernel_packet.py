#!/usr/bin/env python3
"""Real AF_PACKET/TUN verification inside private mount/network namespaces."""
import ctypes as C
import os
import select
from pathlib import Path
import socket
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'debian'))
from ffn_native_packet import library,checked
from ffn_native_aggregate import aggregate_library,Member,Unit
import time


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
        cpus=sorted(os.sched_getaffinity(0))
        checked(lib.ffn_packet_start(ctx,cpus[0],cpus[-1]))
        workers=(C.c_int*8)();checked(lib.ffn_packet_workers(ctx,workers,8))
        assert workers[0]==2 and workers[5]==1, list(workers)
        assert os.sched_getaffinity(workers[3])=={cpus[0]}
        assert os.sched_getaffinity(workers[4])=={cpus[-1]}
        checked(lib.ffn_packet_resume(ctx))
        wire.send(b'\0\x18\0\x1d'+frame)
        wire.send(b'\0\x18\0\x1c'+frame)
        assert capture.recv(4096)==frame
        capture.send(frame)
        expected=b'\1\0\x1c\0'+frame[:12]+bytes(8)+frame[12:]
        for _ in range(32):
            packet=wire.recv(4096)
            if packet==expected:break
        else:raise AssertionError('Native wire transmit not received')
        counts=(C.c_uint64*16)();checked(lib.ffn_packet_counters(ctx,counts,16))
        assert counts[0]==1 and counts[1]>=1 and counts[2]==0, list(counts)
        command('ip','netns','exec','ffnlab','ip','link','set','lab0','down')
        checked(lib.ffn_packet_pause(ctx))
        checked(lib.ffn_packet_resume(ctx))
        command('ip','netns','exec','ffnlab','ip','link','set','lab0','up')
        # The separate observer AF_PACKET socket retains the link-down error.
        # Clear that notification before checking newly delivered traffic.
        capture.getsockopt(socket.SOL_SOCKET,socket.SO_ERROR)
        wire.send(b'\0\x18\0\x1c'+frame)
        assert capture.recv(4096)==frame
        print('PASS pinned native RX/TX workers, socket filter, namespace restoration, TAP ioctl, bidirectional wire framing; '+os.uname().machine)
    finally:
        wire.close();capture.close();lib.ffn_packet_close(ctx)

    # Reopen the same TAP through the aggregate ABI. Independent filtered
    # sockets must keep ordinary data out of the Python LACP control stream.
    lib=aggregate_library(str(Path(__file__).with_name('libffn-packet.so')))
    members=(Member*2)(Member(11,28,0x8001),Member(19,29,0x8101))
    ctx=lib.ffn_aggregate_open(b'testa',b'ffnlab',b'lab0',members,2,1)
    if not ctx:checked(-1)
    sources=(C.c_uint32*4)(28,29,0x8001,0x8101)
    control_fd=lib.ffn_aggregate_control_open(b'testa',sources,4);checked(control_fd)
    control=socket.socket(fileno=control_fd);control.settimeout(1)
    original=os.open('/proc/self/ns/net',os.O_RDONLY);target=os.open('/run/netns/ffnlab',os.O_RDONLY)
    checked(libc.setns(target,0))
    capture=socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(0x88b5));capture.bind(('lab0',0));capture.settimeout(1)
    checked(libc.setns(original,0));os.close(original);os.close(target)
    wire=socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3));wire.bind(('testb',0));wire.settimeout(1)
    wire.setsockopt(263,23,1)
    try:
        rows=(Unit*1)(Unit(tag=4096,mtu=1500));checked(lib.ffn_aggregate_network(ctx,rows,1))
        checked(lib.ffn_aggregate_gates(ctx,3,3,0,int(time.monotonic()*1000)+2500,0,1,None,None))
        checked(lib.ffn_packet_start(ctx,cpus[0],cpus[-1]));checked(lib.ffn_packet_resume(ctx))
        lacp=frame[:12]+b'\x88\x09'+frame[14:]
        lacp_tx=b'\1\0\x1c\0'+lacp[:12]+bytes(8)+lacp[12:]
        assert control.send(lacp_tx)==len(lacp_tx)
        for _ in range(32):
            if wire.recv(4096)==lacp_tx:break
        else:raise AssertionError('Native control socket LACP transmit not received')
        wire.send(b'\0\x18\0\x1e'+frame)  # foreign member
        wire.send(b'\0\x18\0\x1c'+lacp)
        wire.send(b'\0\x18\x81\x01'+frame)  # fixed SPA member alias
        assert control.recv(4096)==b'\0\x18\0\x1c'+lacp
        assert capture.recv(4096)==frame
        control.setblocking(False)
        try:control.recv(4096);raise AssertionError('Data entered the control socket')
        except BlockingIOError:pass
        capture.send(frame)
        expected=[b'\1\0'+bytes([source])+b'\0'+frame[:12]+bytes(8)+frame[12:] for source in (28,29)]
        for _ in range(32):
            if wire.recv(4096) in expected:break
        else:raise AssertionError('Native aggregate wire transmit not received')
        checked(lib.ffn_packet_pause(ctx))
        counts=(C.c_uint64*16)();checked(lib.ffn_packet_counters(ctx,counts,16))
        assert counts[0]==1 and counts[1]>=1,list(counts)
        print('PASS aggregate real socket separation, member aliases, native TAP workers; '+os.uname().machine)
        from ffn_native_aggregate import ControlSocket
        from test_fe100_packet import sample
        receiver=ControlSocket(control,lib,{11:28,19:29},{11:0x8001,19:0x8101})
        receiver.fe100([dict(trunk=24,return_port=20,front=11,in_lif=23,zone=4094,source=28)],int(time.monotonic()*1000)+2000)
        def scoped(name):
            raw=bytearray(sample(name));raw[32:34]=(11<<6).to_bytes(2,'big');return bytes(raw)
        # BPF excludes data exceptions before they enter the control queue.
        wire.send(scoped('control_ndp_sample'))
        time.sleep(.01)
        try:control.recv(4096);raise AssertionError('FE100 data entered control queue')
        except BlockingIOError:pass
        for name in ('control_lacp_sample','control_lldp_sample'):
            raw=scoped(name);wire.send(raw)
            assert select.select([receiver],[],[],1)[0]
            assert receiver.receive()==b'\0\x18\0\x1c'+raw[40:]
        receiver.fe100([],0)
        wire.send(scoped('control_lacp_sample'));time.sleep(.01)
        try:receiver.receive();raise AssertionError('Revoked FE100 control binding accepted')
        except BlockingIOError:pass
        wire.send(b'\0\x18\x80\x01'+lacp)
        assert select.select([receiver],[],[],1)[0]
        assert receiver.receive()==b'\0\x18\0\x1c'+lacp
        print('PASS native FE100 control filter, member normalization, data exclusion and revocation')
    finally:
        lib.ffn_packet_close(ctx);wire.close();capture.close();control.close()


if __name__=='__main__':
    if sys.argv[1:]==['--inside']:inside()
    else:command('unshare','--mount','--net','--propagation','private',sys.executable,str(Path(__file__).resolve()),'--inside')
