#!/usr/bin/env python3
"""Four-port prototype fabric: BCM/FE100 <-> MP <-> pinned SSH <-> DP TAP.

This is a bounded software relay, not a line-rate hardware offload path.
MP owns NIC feature changes; DP owns TAP handles. Both restore on exit.
Owner header reference: pcs/packets/fe100.py, fe100ToOcteonHdr.
"""
import argparse
import collections
import errno
import fcntl
import json
import os
from pathlib import Path
import platform
import select
import signal
import socket
import struct
import subprocess as S
import sys
import time

PORTS = {1: 28, 3: 14, 5: 16, 13: 7}
MAX_FRAME = 1518  # Ethernet MTU 1500, including one 802.1Q tag
MAX_QUEUE = 4 * 1024 * 1024

def decode_cmh(frame):
    if len(frame) < 46 or frame[0] != 0x10 or frame[1] or frame[3] != 3:
        return None
    if frame[22] & 127 or frame[23]:
        return None  # no implementation or ingress exception packets
    slot_port = int.from_bytes(frame[24:26], 'big')
    port = (slot_port >> 6) & 63
    length = int.from_bytes(frame[26:28], 'big') & 0x3fff
    if slot_port >> 12 or port not in PORTS or not 14 <= length <= MAX_FRAME or len(frame) < 32+length:
        return None
    return port, frame[32:32+length]

def encode_itmh(port, frame):
    if port not in PORTS or not 14 <= len(frame) <= MAX_FRAME:
        raise ValueError('invalid egress port or Ethernet length')
    # Direct system-port destination; RAW_DSA egress removes eight bytes.
    return b'\x01'+struct.pack('!H', PORTS[port])+b'\0'+frame[:12]+b'\0'*8+frame[12:]

def record(port, frame):
    if port not in PORTS or not 14 <= len(frame) <= MAX_FRAME:
        raise ValueError('invalid fabric record')
    return struct.pack('!BBH', 1, port, len(frame))+frame

def unpack(buffer):
    while len(buffer) >= 4:
        version, port, length = struct.unpack('!BBH', buffer[:4])
        if version != 1 or port not in PORTS or not 14 <= length <= MAX_FRAME:
            raise ValueError('invalid authenticated fabric stream')
        if len(buffer) < 4+length:
            return
        frame = bytes(buffer[4:4+length])
        del buffer[:4+length]
        yield port, frame

def run(*args):
    return S.check_output(args, text=True)

def tap(port):
    original = os.open('/proc/self/ns/net', os.O_RDONLY)
    target = os.open('/run/netns/ffn-data', os.O_RDONLY)
    fd = None
    try:
        os.setns(target, 0)
        fd = os.open('/dev/net/tun', os.O_RDWR | os.O_NONBLOCK)
        request = 0x800454ca if platform.machine().startswith('mips') else 0x400454ca
        fcntl.ioctl(fd, request, struct.pack('16sH', ('p%d' % port).encode(), 0x1002))
        return fd
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise
    finally:
        os.setns(original, 0)
        os.close(original)
        os.close(target)

def relay(readfd, writefd, endpoints, receive, transmit, seconds):
    incoming, outgoing = bytearray(), bytearray()
    counts = collections.Counter()
    deadline = time.monotonic()+seconds if seconds else float('inf')
    os.set_blocking(readfd, False)
    os.set_blocking(writefd, False)
    while time.monotonic() < deadline:
        ready, writable, _ = select.select([readfd]+list(endpoints), [writefd] if outgoing else [], [], 0.5)
        if writefd in writable:
            try:
                n = os.write(writefd, outgoing)
                del outgoing[:n]
            except BlockingIOError:
                pass
        if readfd in ready:
            data = os.read(readfd, 65536)
            if not data:
                break
            incoming.extend(data)
            for port, frame in unpack(incoming):
                try:
                    transmit(port, frame)
                    counts['to_port_%d' % port] += 1
                except OSError as error:
                    if error.errno in (errno.EAGAIN, errno.ENOBUFS):
                        counts['tx_backpressure_drop'] += 1
                    elif error.errno in (errno.EIO, errno.ENETDOWN):
                        counts['admin_down_drop'] += 1
                    else:
                        raise
        for fd in ready:
            if fd == readfd:
                continue
            item = receive(fd)
            if item is None:
                counts['filtered'] += 1
                continue
            port, frame = item
            if not 14 <= len(frame) <= MAX_FRAME:
                counts['unsupported_length_drop'] += 1
                continue
            message = record(port, frame)
            if len(outgoing)+len(message) > MAX_QUEUE:
                counts['stream_backpressure_drop'] += 1
                continue
            outgoing.extend(message)
            counts['from_port_%d' % port] += 1
    print(json.dumps({'fabric_counters': dict(counts)}), file=sys.stderr, flush=True)

def dp(seconds):
    handles = {}
    state = Path('/run/ffn-fabric.json')
    try:
        with open('/run/ffn-network.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            links = json.loads(run('ip', '-n', 'ffn-data', '-j', 'link'))
            links = {item['ifname']: item for item in links}
            for port in PORTS:
                if 'p%d' % port not in links or links['p%d' % port]['mtu'] > 1500:
                    raise ValueError('fabric requires existing p%d with MTU <= 1500' % port)
            for port in PORTS:
                handles[port] = tap(port)
            state.write_text(json.dumps({'pid': os.getpid(), 'ports': list(PORTS), 'transport': 'MP SSH relay', 'max_mtu': 1500}))
        byfd = {fd: port for port, fd in handles.items()}
        print('FABRIC_DP_READY', file=sys.stderr, flush=True)
        relay(0, 1, byfd, lambda fd: (byfd[fd], os.read(fd, 65535)),
              lambda port, frame: os.write(handles[port], frame), seconds)
    finally:
        for fd in handles.values():
            os.close(fd)
        state.unlink(missing_ok=True)

def mp(seconds):
    rx = tx = child = None
    changed_rx = changed_up = False
    old_mtu = {}
    try:
        flags = int(Path('/sys/class/net/enp8s0f0/flags').read_text(), 16)
        for interface in ('enp8s0f0', 'enp8s0f1'):
            mtu = int(Path('/sys/class/net', interface, 'mtu').read_text())
            if mtu < 1600:
                run('ip','link','set',interface,'mtu','1600')
                old_mtu[interface] = mtu
        if not flags & 1:
            run('ip', 'link', 'set', 'enp8s0f0', 'up')
            changed_up = True
        feature = next(x.strip() for x in run('ethtool','-k','enp8s0f1').splitlines() if x.strip().startswith('rx-all:'))
        if feature == 'rx-all: off':
            run('ethtool','-K','enp8s0f1','rx-all','on')
            changed_rx = True
        elif not feature.startswith('rx-all: on'):
            raise RuntimeError('rx-all unavailable')
        time.sleep(3)
        rx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
        rx.bind(('enp8s0f1', 0))
        rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, MAX_QUEUE)
        rx.setsockopt(263, 1, struct.pack('IHH8s', socket.if_nametoindex('enp8s0f1'), 1, 0, b''))
        tx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
        tx.bind(('enp8s0f0', 0))
        tx.setblocking(False)
        child = S.Popen(['ssh','-T','-o','BatchMode=yes','-o','ConnectTimeout=10',
            '-o','ServerAliveInterval=10','-o','ServerAliveCountMax=3',
            '-o','UserKnownHostsFile=/etc/ffn-ngfw/plane_boot_known_hosts',
            '-o','ProxyCommand=ssh -F /etc/ffn-ngfw/ssh-cp.conf -W %h:%p ffn-cp',
            'root@127.1.2.2','python3 /usr/local/sbin/ffn_fabric.py dp'],
            stdin=S.PIPE, stdout=S.PIPE, bufsize=0)
        def receive(fd):
            frame, addr = rx.recvfrom(65536)
            return None if addr[2] == socket.PACKET_OUTGOING else decode_cmh(frame)
        relay(child.stdout.fileno(), child.stdin.fileno(), {rx.fileno(): rx}, receive,
              lambda port, frame: tx.send(encode_itmh(port, frame)), seconds)
    finally:
        if child:
            child.stdin.close()
            try:
                child.wait(timeout=5)
            except S.TimeoutExpired:
                child.terminate()
                child.wait(timeout=5)
        if rx: rx.close()
        if tx: tx.close()
        if changed_rx: run('ethtool','-K','enp8s0f1','rx-all','off')
        if changed_up: run('ip','link','set','enp8s0f0','down')
        for interface, mtu in old_mtu.items():
            run('ip','link','set',interface,'mtu',str(mtu))
    if child and child.returncode:
        raise RuntimeError('DP relay exited with status %s' % child.returncode)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('role', choices=('mp','dp'))
    p.add_argument('--seconds', type=int, default=0)
    args = p.parse_args()
    if not 0 <= args.seconds <= 3600:
        p.error('seconds must be 0..3600 (0 runs until stopped)')
    with open('/run/ffn-fabric.lock','w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        signal.signal(signal.SIGTERM, lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
        try:
            (mp if args.role == 'mp' else dp)(args.seconds)
        except KeyboardInterrupt:
            pass

if __name__ == '__main__': main()
