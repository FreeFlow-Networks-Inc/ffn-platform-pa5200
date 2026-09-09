#!/usr/bin/env python3
"""MP lab: repeated tagged Ethernet bursts, capture returned payloads.

Uses a dedicated internal NIC. No addresses or routes are configured.
Requires an explicitly configured return path on the switch. Optional IPv4/UDP
packets use benchmark addresses and are injected as raw Ethernet frames.
"""
import argparse
import atexit
import collections
import json
import socket
import signal
import struct
import subprocess
import threading
import time
import uuid

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('interface')
p.add_argument('--tx-interface', help='separate dedicated injection NIC')
p.add_argument('--itmh-system-port', type=int, help='prepend Jericho direct-system-port ITMH')
p.add_argument('--count', type=int, default=1000)
p.add_argument('--bursts', type=int, default=3)
p.add_argument('--dsa-padding', action='store_true', help='insert the 8-byte tag removed by front-port RAW_DSA egress')
p.add_argument('--ipv4', action='store_true', help='use checksum-valid IPv4/UDP test packets')
p.add_argument('--rx-all', action='store_true', help='temporarily accept FE100 message frames rejected by ixgbe length checks')
args = p.parse_args()
if args.itmh_system_port is not None and not 0 <= args.itmh_system_port <= 65535:
    p.error('ITMH system port must be 0..65535')
if not 1 <= args.count <= 10000 or not 1 <= args.bursts <= 10:
    p.error('count must be 1..10000 and bursts 1..10')
if args.rx_all:
    features = subprocess.check_output(['ethtool', '-k', args.interface], text=True)
    feature = next((line.strip() for line in features.splitlines() if line.strip().startswith('rx-all:')), '')
    if feature == 'rx-all: off':
        subprocess.run(['ethtool', '-K', args.interface, 'rx-all', 'on'], check=True)
        def restore_rx_all():
            subprocess.run(['ethtool', '-K', args.interface, 'rx-all', 'off'], check=True)
        atexit.register(restore_rx_all)
        # ixgbe resets the NIC when this feature changes. Let link recover
        # before counting test packets; the first immediate burst can be lost.
        time.sleep(2)
    elif not feature.startswith('rx-all: on'):
        p.error('NIC does not support changing rx-all')
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
marker = b'FFN-TEST-' + uuid.uuid4().bytes
mac = bytes.fromhex(open('/sys/class/net/' + args.interface + '/address').read().strip().replace(':', ''))
rx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
rx.bind((args.interface, 0)); rx.settimeout(0.1)
membership = struct.pack('IHH8s', socket.if_nametoindex(args.interface), 1, 0, b'')
rx.setsockopt(263, 1, membership)  # PACKET_ADD_MEMBERSHIP, PROMISC; scoped to socket
tx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
tx.bind((args.tx_interface or args.interface, 0))
received = collections.Counter()
bad = []
offsets = collections.Counter()
sample_prefixes = []
stop = threading.Event()
def checksum(data):
    if len(data) & 1: data += b'\0'
    total=sum(struct.unpack('!%dH'%(len(data)//2),data))
    while total >> 16: total=(total & 65535)+(total >> 16)
    return (~total) & 65535

def network_payload(payload,seq):
    if not args.ipv4:return b'\x88\xb5'+payload
    source=socket.inet_aton('198.18.0.1');destination=socket.inet_aton('198.18.0.2')
    udp=struct.pack('!HHHH',49000,49001,8+len(payload),0)+payload
    pseudo=source+destination+struct.pack('!BBH',0,17,len(udp))
    udp=udp[:6]+struct.pack('!H',checksum(pseudo+udp) or 65535)+udp[8:]
    ip=struct.pack('!BBHHHBBH4s4s',0x45,0,20+len(udp),seq&65535,0x4000,64,17,0,source,destination)
    ip=ip[:10]+struct.pack('!H',checksum(ip))+ip[12:]
    return b'\x08\x00'+ip+udp
def capture():
    while not stop.is_set():
        try: frame, address = rx.recvfrom(65535)
        except socket.timeout: continue
        if address[2] == socket.PACKET_OUTGOING: continue
        offset = frame.find(marker)
        if offset < 0: continue
        if not sample_prefixes:
            sample_prefixes.append(frame[:64].hex())
        payload = frame[offset + len(marker):]
        if len(payload) < 8:
            bad.append('truncated'); continue
        burst, seq = struct.unpack('!II', payload[:8])
        expected = bytes((i + seq) & 255 for i in range(192))
        if payload[8:200] != expected or not (0 <= burst < args.bursts and 0 <= seq < args.count):
            bad.append((burst, seq)); continue
        received[burst, seq] += 1
        offsets[offset] += 1
thread = threading.Thread(target=capture)
thread.start()
try:
    for burst in range(args.bursts):
        for seq in range(args.count):
            payload = marker + struct.pack('!II', burst, seq) + bytes((i + seq) & 255 for i in range(192))
            itmh = (b'\x01' + struct.pack('!H', args.itmh_system_port) + b'\x00'
                    if args.itmh_system_port is not None else b'')
            tx.send(itmh + b'\xff' * 6 + mac + (b'\x00' * 8 if args.dsa_padding else b'') + network_payload(payload,seq))
            time.sleep(0.0005)
        time.sleep(2)
        print(json.dumps({'burst': burst, 'sent': args.count,
                          'returned': sum((burst, seq) in received for seq in range(args.count))}), flush=True)
finally:
    stop.set(); thread.join(); rx.close(); tx.close()
missing = sum((burst, seq) not in received for burst in range(args.bursts) for seq in range(args.count))
duplicates = sum(n - 1 for n in received.values())
print(json.dumps({'missing': missing, 'duplicates': duplicates, 'corrupt': len(bad),
                  'payload_offsets': dict(offsets), 'bursts': args.bursts, 'count': args.count,
                  'sample_frame_prefixes': sample_prefixes}), flush=True)
raise SystemExit(1 if missing or duplicates or bad else 0)
