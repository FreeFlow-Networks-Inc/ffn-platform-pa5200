#!/usr/bin/env python3
"""MP: verify the exact commissioning BCM rule, plus a nonmatching control.
Requires cables 1--3 and 5--13. No network configuration changes.
"""
import collections
import json
import socket
import struct
import sys
import threading
import time
import uuid
sys.path.insert(0, '/usr/local/sbin')
from ffn_fabric import decode_cmh, encode_itmh


def test(source, expected, rewritten_destination=None, ipv4=False, unicast=False):
    token = uuid.uuid4().bytes
    frames = {i: b'\xff'*6+bytes.fromhex(source)+b'\x88\xb5'+token+struct.pack('!I', i)+bytes(range(128)) for i in range(300)}
    if unicast:
        frames = {i: bytes.fromhex('025220abcdcc')+f[6:] for i,f in frames.items()}
    if ipv4:
        for i, frame in frames.items():
            payload = frame[14:]
            header = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 28+len(payload), i, 0,
                                 64, 17, 0, socket.inet_aton('198.18.99.1'), socket.inet_aton('198.18.99.2'))
            total = sum(struct.unpack('!10H', header))
            while total >> 16: total = (total & 65535)+(total >> 16)
            header = header[:10]+struct.pack('!H', (~total)&65535)+header[12:]
            frames[i] = frame[:12]+b'\x08\x00'+header+struct.pack('!HHHH', 5220, 5221, 8+len(payload), 0)+payload
    seen = collections.defaultdict(collections.Counter)
    envelopes = collections.Counter()
    bad = []
    stop = threading.Event()
    rx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
    rx.bind(('enp8s0f1', 0))
    rx.settimeout(.1)
    rx.setsockopt(263, 1, struct.pack('IHH8s', socket.if_nametoindex('enp8s0f1'), 1, 0, b''))
    tx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    tx.bind(('enp8s0f0', 0))

    def capture():
        while not stop.is_set():
            try:
                data, addr = rx.recvfrom(65535)
            except socket.timeout:
                continue
            if addr[2] == socket.PACKET_OUTGOING:
                continue
            marker = data.find(token)
            if marker >= 0:
                envelopes[(marker, data[:4].hex(), data[22:24].hex(), len(data), data[32:38].hex())] += 1
            item = decode_cmh(data)
            if not item:
                continue
            port, frame = item
            offset = frame.find(token)
            if offset < 0:
                continue
            seq = int.from_bytes(frame[offset+16:offset+20], 'big')
            seen[port][seq] += 1
            wanted = frames.get(seq)
            if wanted is not None and rewritten_destination:
                wanted = bytes.fromhex(rewritten_destination) + wanted[6:]
            if frame != wanted:
                bad.append(seq)

    thread = threading.Thread(target=capture)
    thread.start()
    try:
        for frame in frames.values():
            tx.send(encode_itmh(3, frame))
            time.sleep(.005)
        time.sleep(3)
    finally:
        stop.set()
        thread.join()
        rx.close()
        tx.close()
    result = {'source': source, 'sent': 300, 'captured_ports': {p: len(v) for p, v in seen.items()},
              'corrupt': len(bad), 'expect_hardware_redirect': expected,
              'rewritten_destination': rewritten_destination,
              'ipv4': ipv4,
              'unicast': unicast,
              'raw_envelopes': [{'marker_offset': k[0], 'prefix': k[1],
                                 'exception_bytes': k[2], 'length': k[3], 'destination': k[4], 'count': v}
                                for k, v in envelopes.items()]}
    if expected:
        result['passed'] = len(seen[13]) == 300 and sum(seen[13].values()) == 300 and not seen[1] and not bad
    else:
        result['passed'] = len(seen[1]) == 300 and not seen[13] and not bad
    print(json.dumps(result), flush=True)
    assert result['passed'], result


if __name__ == '__main__':
    test('025220abcd91', '--rule-installed' in sys.argv)
    test('025220abcd92', False)
