#!/usr/bin/env python3
"""Hardware Linux-stack tests through TAP, without claiming physical-port IO.
Run on DP outside ffn-data. Requires the supplied isolated network-lab.json.
"""
import fcntl
import json
import os
import platform
import select
import struct
import subprocess as S
import time
import uuid

CTL = '/usr/local/sbin/ffn_network.py'
NS = 'ffn-data'

def command(*args):
    return S.check_output(args, text=True)

def control(action, request=None):
    p = S.run(['python3', CTL, action], input=json.dumps(request) if request else None,
              text=True, capture_output=True, check=True)
    return json.loads(p.stdout)

def nip(*args):
    return command('ip', '-n', NS, *args)

def checksum(data):
    data += b'\0' * (len(data) % 2)
    total = sum(struct.unpack('!%dH' % (len(data)//2), data))
    while total >> 16:
        total = (total & 65535) + (total >> 16)
    return (~total) & 65535

def mac(dev):
    return bytes.fromhex(json.loads(nip('-j', 'link', 'show', dev))[0]['address'].replace(':', ''))

def tap(name):
    original = os.open('/proc/self/ns/net', os.O_RDONLY)
    target = os.open('/run/netns/' + NS, os.O_RDONLY)
    try:
        os.setns(target, 0)
        fd = os.open('/dev/net/tun', os.O_RDWR | os.O_NONBLOCK)
        req = 0x800454ca if platform.machine().startswith('mips') else 0x400454ca
        fcntl.ioctl(fd, req, struct.pack('16sH', name.encode(), 0x1002))
        return fd
    finally:
        os.setns(original, 0)
        os.close(original)
        os.close(target)

def drain(fd):
    while select.select([fd], [], [], 0)[0]:
        os.read(fd, 16384)

def receive(fd, marker, timeout=2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if select.select([fd], [], [], max(0, end-time.monotonic()))[0]:
            frame = os.read(fd, 16384)
            if marker in frame:
                return frame
    return None

def ipv4(source, destination, marker, ttl=64):
    import socket
    udp = struct.pack('!HHHH', 40000, 40001, 8+len(marker), 0) + marker
    h = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20+len(udp), 1, 0x4000, ttl, 17, 0,
                    socket.inet_aton(source), socket.inet_aton(destination))
    return h[:10]+struct.pack('!H', checksum(h))+h[12:]+udp

def route_test(inp, out, source, destination, count=100):
    gateway = mac(inp)
    targetmac = '02:52:20:00:00:02'
    nip('neigh', 'replace', destination, 'lladdr', targetmac, 'nud', 'permanent', 'dev', out)
    try:
        drain(fds[out])
        for sequence in range(count):
            marker = token + struct.pack('!I', sequence)
            payload = ipv4(source, destination, marker)
            os.write(fds[inp], gateway + b'\x02\x52\x20\x00\x00\x01' + b'\x08\x00' + payload)
            frame = receive(fds[out], marker)
            assert frame is not None, ('missing routed frame', sequence)
            assert frame[:6] == bytes.fromhex(targetmac.replace(':', ''))
            assert frame[6:12] == mac(out) and frame[22] == 63 and checksum(frame[14:34]) == 0
            assert frame[34:] == payload[20:], 'UDP payload modified'
        print(json.dumps({'test': 'ipv4-routing', 'input': inp, 'output': out, 'packets': count,
                          'ttl_decrement': True, 'checksum_and_mac_rewrite': True}), flush=True)
    finally:
        nip('neigh', 'del', destination, 'dev', out)

def ipv6_test():
    import socket
    source = socket.inet_pton(socket.AF_INET6, 'fd52:20:1::2')
    destination = socket.inet_pton(socket.AF_INET6, 'fd52:20:2::2')
    targetmac = '02:52:20:00:00:02'
    nip('-6', 'neigh', 'replace', 'fd52:20:2::2', 'lladdr', targetmac, 'nud', 'permanent', 'dev', 'p13')
    try:
        for sequence in range(100):
            marker = token + b'v6' + struct.pack('!I', sequence)
            udp = struct.pack('!HHHH',40000,40001,8+len(marker),0)+marker
            pseudo = source+destination+struct.pack('!I3xB',len(udp),17)
            udp = udp[:6]+struct.pack('!H',checksum(pseudo+udp) or 65535)+udp[8:]
            packet = struct.pack('!IHBB',0x60000000,len(udp),17,64)+source+destination+udp
            os.write(fds['p5'],mac('p5')+b'\x02\x52\x20\x00\x00\x01'+b'\x86\xdd'+packet)
            frame = receive(fds['p13'],marker)
            assert frame and frame[21] == 63 and frame[54:] == udp, 'IPv6 routing failed'
        print(json.dumps({'test':'ipv6-routing','packets':100,'hop_limit_decrement':True}),flush=True)
    finally:
        nip('-6','neigh','del','fd52:20:2::2','dev','p13')

def arp_test():
    import socket
    peer = b'\x02\x52\x20\x00\x00\x71'
    gateway = socket.inet_aton('198.18.1.1')
    source = socket.inet_aton('198.18.1.2')
    request = struct.pack('!HHBBH',1,0x0800,6,4,1)+peer+source+b'\0'*6+gateway
    drain(fds['p5'])
    os.write(fds['p5'], b'\xff'*6+peer+b'\x08\x06'+request)
    reply = receive(fds['p5'], peer)
    assert reply and reply[12:14] == b'\x08\x06' and reply[20:22] == b'\0\x02'
    assert reply[22:28] == mac('p5') and reply[28:32] == gateway
    print(json.dumps({'test':'arp-gateway-resolution','passed':True}),flush=True)

cfg = control('status')['config']
assert cfg['ports']['p1'] == {'mode':'l2','vlans':[100],'pvid':100}, 'requires isolated lab config'
assert cfg['ports']['p3'] == cfg['ports']['p1'], 'requires isolated lab config'
fds = {}
token = uuid.uuid4().bytes
changed = False
try:
    fds = {p: tap(p) for p in ('p1','p3','p5','p13')}
    import sys
    if '--arp-only' in sys.argv:
        arp_test()
        raise SystemExit(0)
    # STP is enabled in production configuration: wait for normal forwarding.
    time.sleep(32)
    drain(fds['p3'])
    for sequence in range(100):
        marker = token + struct.pack('!I', sequence)
        frame = b'\xff'*6 + b'\x02\x52\x20\x00\x00\x01' + b'\x88\xb5' + marker + b'x'*40
        os.write(fds['p1'], frame)
        assert receive(fds['p3'], marker) == frame, 'L2 payload mismatch'
    print(json.dumps({'test':'l2-access-vlan100','packets':100,'payload_integrity':True}), flush=True)
    entries = json.loads(command('ip','netns','exec',NS,'bridge','-j','fdb','show','dev','p1'))
    assert any(e['mac'] == '02:52:20:00:00:01' and e.get('vlan') == 100 for e in entries), 'MAC learning failed'
    print(json.dumps({'test':'l2-mac-learning','vlan':100,'passed':True}),flush=True)
    arp_test()
    route_test('p5','p13','198.18.1.2','198.18.2.2')
    ipv6_test()
    before = json.loads(nip('-j', 'link', 'show', 'p5'))[0]
    revision = control('status')['config']['revision']
    control('patch', {'revision':revision,'ports':{'p3':{'mode':'l2','vlans':[200],'pvid':200}}})
    changed = True
    # STP delay must not masquerade as VLAN isolation. Wait for and check
    # real forwarding state rather than overriding an STP-owned port.
    time.sleep(32)
    state = json.loads(command('ip','netns','exec',NS,'bridge','-j','link','show','dev','p3'))
    assert state[0]['state'] == 'forwarding', state
    drain(fds['p3'])
    marker = token+b'isolated'
    os.write(fds['p1'], b'\xff'*6+b'\x02\x52\x20\x00\x00\x01'+b'\x88\xb5'+marker+b'x'*40)
    assert receive(fds['p3'],marker,1) is None, 'VLAN leaked'
    print(json.dumps({'test':'vlan100-to-vlan200-isolation','passed':True}),flush=True)
    revision = control('status')['config']['revision']
    control('patch', {'revision':revision,'ports':{
        'p1':{'mode':'l3','addresses':['198.18.3.1/24']},
        'p3':{'mode':'l3','addresses':['198.18.4.1/24']}}})
    route_test('p1','p3','198.18.3.2','198.18.4.2')
    after = json.loads(nip('-j', 'link', 'show', 'p5'))[0]
    assert before['ifindex'] == after['ifindex'] and before['address'] == after['address']
    route_test('p5','p13','198.18.1.2','198.18.2.2',10)
    print(json.dumps({'test':'runtime-l2-to-l3','unchanged_port_preserved':True}),flush=True)
finally:
    if changed:
        revision = control('status')['config']['revision']
        control('patch', {'revision':revision,'ports':{p:cfg['ports'][p] for p in ('p1','p3')}})
    for fd in fds.values():
        os.close(fd)
