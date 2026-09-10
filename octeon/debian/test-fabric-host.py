#!/usr/bin/env python3
"""Physical ARP/NDP and ICMP tests for the saved p13 lab subnet.

Requires cable 5--13 and the four-port fabric. Emulates a host on front13
through front5 injection; checks replies captured after front13 transmission.
Uses dynamic neighbor discovery, never installs permanent neighbor entries.
Run only after other commissioning tests have restored port configuration.
"""
import importlib.util
import json
import socket
import struct
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, '/usr/local/sbin')
from ffn_fabric import decode_cmh, encode_itmh

spec = importlib.util.spec_from_file_location('matrix', Path(__file__).with_name('test-fabric-matrix.py'))
matrix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(matrix)
checksum, dp, control = matrix.checksum, matrix.dp, matrix.control


def main():
    state = control('status')
    port = state['config']['ports']['p13']
    assert state['running'] and port['mode'] == 'l3'
    assert {'198.18.2.1/24', 'fd52:20:2::1/64'} <= set(port['addresses'])
    local_mac = bytes.fromhex(next(i['address'] for i in state['interfaces']
                                   if i['ifname'] == 'p13').replace(':', ''))
    peer_mac = bytes.fromhex('025220abcd93')
    addresses = [(4, '198.18.2.223'), (6, 'fd52:20:2::223')]
    # Never overwrite a preexisting neighbor, including one in FAILED state.
    for version, address in addresses:
        assert not json.loads(dp(f'ip -{version} -n ffn-data -j neigh show to {address} dev p13'))
    rx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
    tx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    try:
        rx.bind(('enp8s0f1', 0)); rx.settimeout(.2)
        rx.setsockopt(263, 1, struct.pack('IHH8s', socket.if_nametoindex('enp8s0f1'), 1, 0, b''))
        tx.bind(('enp8s0f0', 0))

        def exchange(name, frame, matches):
            tx.send(encode_itmh(5, frame))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    raw, addr = rx.recvfrom(65535)
                except socket.timeout:
                    continue
                if addr[2] == socket.PACKET_OUTGOING:
                    continue
                item = decode_cmh(raw)
                if item and item[0] == 5 and matches(item[1]):
                    return
            raise RuntimeError('no valid physical reply: ' + name)

        v4peer = socket.inet_aton(addresses[0][1])
        v4local = socket.inet_aton('198.18.2.1')
        arp = struct.pack('!HHBBH', 1, 0x800, 6, 4, 1)+peer_mac+v4peer+bytes(6)+v4local
        expected_arp = (peer_mac+local_mac+b'\x08\x06'+struct.pack('!HHBBH', 1, 0x800, 6, 4, 2)
                        +local_mac+v4local+peer_mac+v4peer)
        exchange('arp', bytes.fromhex('ffffffffffff')+peer_mac+b'\x08\x06'+arp,
                 lambda f: f[:42] == expected_arp)
        print(json.dumps({'test': 'physical-arp-reply', 'passed': True}), flush=True)

        v6peer = socket.inet_pton(socket.AF_INET6, addresses[1][1])
        v6local = socket.inet_pton(socket.AF_INET6, 'fd52:20:2::1')
        solicited = socket.inet_pton(socket.AF_INET6, 'ff02::1:ff00:1')

        def ipv6(payload, src, dst, hops):
            return struct.pack('!IHBB', 0x60000000, len(payload), 58, hops)+src+dst+payload

        def icmp6(payload, src, dst):
            pseudo = src+dst+struct.pack('!I3xB', len(payload), 58)
            return payload[:2]+struct.pack('!H', checksum(pseudo+payload))+payload[4:]

        ns = icmp6(bytes([135, 0, 0, 0])+bytes(4)+v6local+bytes([1, 1])+peer_mac, v6peer, solicited)

        def valid_na(f):
            if len(f) < 86 or f[:14] != peer_mac+local_mac+b'\x86\xdd':
                return False
            length = int.from_bytes(f[18:20], 'big')
            body = f[54:54+length]
            return (f[20:22] == bytes([58, 255]) and f[22:38] == v6local and f[38:54] == v6peer
                    and len(body) >= 32 and body[0:2] == bytes([136, 0])
                    and body[4] & 0x40 and body[8:24] == v6local
                    and body[24:32] == bytes([2, 1])+local_mac
                    and checksum(v6local+v6peer+struct.pack('!I3xB', length, 58)+body) == 0)

        exchange('ndp', bytes.fromhex('3333ff000001')+peer_mac+b'\x86\xdd'
                 +ipv6(ns, v6peer, solicited, 255), valid_na)
        print(json.dumps({'test': 'physical-ndp-reply', 'passed': True}), flush=True)

        for version, source in addresses:
            for seq in range(20):
                payload = uuid.uuid4().bytes + struct.pack('!I', seq) + bytes(range(64))
                body = struct.pack('!BBHHH', 8 if version == 4 else 128, 0, 0, 0x5220, seq)+payload
                if version == 4:
                    body = body[:2]+struct.pack('!H', checksum(body))+body[4:]
                    h = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20+len(body), seq, 0, 64, 1, 0, v4peer, v4local)
                    frame = local_mac+peer_mac+b'\x08\x00'+h[:10]+struct.pack('!H', checksum(h))+h[12:]+body
                else:
                    body = icmp6(body, v6peer, v6local)
                    frame = local_mac+peer_mac+b'\x86\xdd'+ipv6(body, v6peer, v6local, 64)

                def valid_echo(f):
                    if f[:12] != peer_mac+local_mac:
                        return False
                    if version == 4:
                        if len(f) < 42 or f[12:14] != b'\x08\x00' or f[14] >> 4 != 4:
                            return False
                        ihl = (f[14] & 15)*4
                        length = int.from_bytes(f[16:18], 'big')
                        reply = f[14+ihl:14+length]
                        valid = (ihl >= 20 and len(f) >= 14+length and f[23] == 1
                                 and f[26:30] == v4local and f[30:34] == v4peer
                                 and checksum(f[14:14+ihl]) == 0 and checksum(reply) == 0)
                    else:
                        if len(f) < 62 or f[12:14] != b'\x86\xdd':
                            return False
                        length = int.from_bytes(f[18:20], 'big')
                        reply = f[54:54+length]
                        valid = (len(f) >= 54+length and f[20] == 58
                                 and f[22:38] == v6local and f[38:54] == v6peer
                                 and checksum(v6local+v6peer+struct.pack('!I3xB', length, 58)+reply) == 0)
                    return (valid and reply[:2] == bytes([0 if version == 4 else 129, 0])
                            and reply[4:] == struct.pack('!HH', 0x5220, seq)+payload)

                exchange(f'ipv{version}-echo-{seq}', frame, valid_echo)
            print(json.dumps({'test': f'physical-ipv{version}-echo-dynamic-neighbor',
                              'sent': 20, 'replies': 20, 'passed': True}), flush=True)
        assert control('status')['config'] == state['config']
    finally:
        rx.close(); tx.close()
        for version, address in addresses:
            # Only remove the lab neighbor if it has our test MAC.
            current = json.loads(dp(f'ip -{version} -n ffn-data -j neigh show to {address} dev p13'))
            if any(n.get('lladdr') == '02:52:20:ab:cd:93' for n in current):
                dp(f'ip -{version} -n ffn-data neigh del {address} dev p13')


if __name__ == '__main__':
    main()
