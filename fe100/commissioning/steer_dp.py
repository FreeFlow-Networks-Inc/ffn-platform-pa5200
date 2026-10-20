#!/usr/bin/env python3
"""Divert exactly one conntrack session from the DP's software path into the FE100.

For the flow's two directions, frames arriving on the LAN VLAN device and on the WAN
device are dropped by tc before the IP stack and re-sent through the packet trunk to
switch port 3 (the FE100 link) with the VLAN tag restored, like the lab injector. Every
tc change is undone on exit or after --seconds. Counts go to stdout as JSON lines.

  steer_dp.py --lan-dev ae1.69 --lan-vlan 69 --wan-dev p5 --proto 6 \
      --src A --sport P --dst B --dport Q --xsrc T --xsport U [--seconds 90] [--dsa-vlan]
"""
import argparse
import json
import os
import select
import socket
import struct
import subprocess
import sys
import time

sys.path[:0] = ['/usr/local/sbin', '/usr/local/lib/ffn']


def tc(args, check=True):
    return subprocess.run(['ip', 'netns', 'exec', 'ffn-data', 'tc'] + args, capture_output=True, text=True, check=check)


def parse(frame):
    """(src, sport, dst, dport, proto) of an untagged IPv4 TCP/UDP frame, else None."""
    if len(frame) < 38 or frame[12:14] != b'\x08\x00' or frame[14] >> 4 != 4:
        return None
    ihl = (frame[14] & 15) * 4
    proto = frame[23]
    if proto not in (6, 17) or len(frame) < 14 + ihl + 4:
        return None
    src = socket.inet_ntoa(frame[26:30]); dst = socket.inet_ntoa(frame[30:34])
    sport, dport = struct.unpack('!HH', frame[14 + ihl:14 + ihl + 4])
    return src, sport, dst, dport, proto


def envelope(frame, dsa_word=0):
    frame = frame.ljust(60, b'\0')
    return b'\x01' + (3).to_bytes(2, 'big') + b'\0' + frame[:12] + bytes(4) + dsa_word.to_bytes(4, 'big') + frame[12:]


def main():
    p = argparse.ArgumentParser()
    for name in ('lan-dev', 'wan-dev', 'src', 'dst', 'xsrc'):
        p.add_argument('--' + name, required=True)
    for name in ('lan-vlan', 'proto', 'sport', 'dport', 'xsport'):
        p.add_argument('--' + name, type=int, required=True)
    p.add_argument('--seconds', type=int, default=90)
    p.add_argument('--dsa-vlan', action='store_true', help='also carry tagged+VID in the trunk DSA word (bytes 20..23)')
    p.add_argument('--inject-port', type=int, default=0, help='front port to inject through (its MAC loop feeds the FE100); 0 = switch port 3 directly')
    a = p.parse_args()
    if a.inject_port:
        from ffn_dp_packet_transport import encode as front_encode
    forward = (a.src, a.sport, a.dst, a.dport, a.proto)          # LAN -> WAN as seen on the LAN device
    reverse = (a.dst, a.dport, a.xsrc, a.xsport, a.proto)        # WAN -> LAN replies to the translated tuple on the WAN device
    l4 = 'tcp' if a.proto == 6 else 'udp'
    # trunk socket in the root namespace first, then move into the data namespace for the taps
    trunk = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3)); trunk.bind(('ffnpkt0', 0))
    ns = os.open('/run/netns/ffn-data', os.O_RDONLY)
    os.setns(ns, os.CLONE_NEWNET); os.close(ns)
    taps = {}
    for dev in (a.lan_dev, a.wan_dev):
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
        s.setsockopt(socket.SOL_SOCKET, 33, 8 * 1024 * 1024); s.bind((dev, 0)); s.setblocking(False); taps[s] = dev
    added = []
    counts = dict(lan_seen=0, lan_sent=0, wan_seen=0, wan_sent=0, other=0)
    try:
        for dev in (a.lan_dev, a.wan_dev):
            if 'clsact' not in tc(['qdisc', 'show', 'dev', dev]).stdout:
                tc(['qdisc', 'add', 'dev', dev, 'clsact']); added.append(('qdisc', dev))
        tc(['filter', 'add', 'dev', a.lan_dev, 'ingress', 'protocol', 'ip', 'pref', '7', 'flower', 'skip_hw', 'ip_proto', l4,
            'src_ip', a.src, 'dst_ip', a.dst, 'src_port', str(a.sport), 'dst_port', str(a.dport), 'action', 'drop'])
        added.append(('filter', a.lan_dev))
        tc(['filter', 'add', 'dev', a.wan_dev, 'ingress', 'protocol', 'ip', 'pref', '7', 'flower', 'skip_hw', 'ip_proto', l4,
            'src_ip', a.dst, 'dst_ip', a.xsrc, 'src_port', str(a.dport), 'dst_port', str(a.xsport), 'action', 'drop'])
        added.append(('filter', a.wan_dev))
        print(json.dumps(dict(event='steering', forward=forward, reverse=reverse, added=added)), flush=True)
        deadline = time.monotonic() + a.seconds; last = time.monotonic()
        while time.monotonic() < deadline:
            ready = select.select(list(taps), [], [], .2)[0]
            for s in ready:
                try:
                    raw, addr = s.recvfrom(65536)
                except BlockingIOError:
                    continue
                if addr[2] == socket.PACKET_OUTGOING:
                    continue
                tup = parse(raw)
                if tup is None:
                    continue
                dev = taps[s]
                if dev == a.lan_dev and tup == forward:
                    counts['lan_seen'] += 1
                    tagged = raw[:12] + b'\x81\x00' + a.lan_vlan.to_bytes(2, 'big') + raw[12:]
                    word = ((3 << 30) | (1 << 29) | a.lan_vlan) if a.dsa_vlan else 0
                    trunk.send(front_encode(a.inject_port, tagged) if a.inject_port else envelope(tagged, word)); counts['lan_sent'] += 1
                elif dev == a.wan_dev and tup == reverse:
                    counts['wan_seen'] += 1
                    trunk.send(front_encode(a.inject_port, raw) if a.inject_port else envelope(raw, (3 << 30) if a.dsa_vlan else 0)); counts['wan_sent'] += 1
                else:
                    counts['other'] += 1
            if time.monotonic() - last >= 5:
                last = time.monotonic(); print(json.dumps(dict(event='counts', **counts)), flush=True)
    finally:
        errors = []
        for kind, dev in reversed(added):
            try:
                if kind == 'filter':
                    tc(['filter', 'del', 'dev', dev, 'ingress', 'pref', '7'])
                else:
                    tc(['qdisc', 'del', 'dev', dev, 'clsact'])
            except Exception as error:
                errors.append('%s %s: %s' % (kind, dev, error))
        print(json.dumps(dict(event='stopped', counts=counts, cleanup_errors=errors)), flush=True)


if __name__ == '__main__':
    main()
