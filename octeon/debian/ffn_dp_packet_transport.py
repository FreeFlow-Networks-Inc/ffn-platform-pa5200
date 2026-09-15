#!/usr/bin/env python3
"""Direct DP Linux packet transport for a commissioned BCM/FE100 trunk.

No MP/SSH packet relay. Uses existing pN TAPs in ffn-data and the existing
inspection engine. Trunk provisioning, RX-all and MTU are commissioning tasks;
this process does not change them or claim hardware flow acceleration.
"""
import argparse
import collections
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import socket
import struct
import time

from ffn_fabric import tap, run, MAX_FRAME

FRONT = dict(enumerate((28,13,14,15,16,1,18,19,6,21,22,23,7,11,36,27,10,29,30,31,32,33,34,35), 1))


def decode(frame, ports):
    # FE100 -> OCTEON CMH, verified in owner's pcs/packets/fe100.py.
    # Only the commissioned SYSPORT envelope is accepted. Exceptions and
    # session/control messages must never be injected as Ethernet packets.
    if len(frame) < 46 or frame[:2] != b'\x10\0' or frame[3] != 3:
        return None
    if frame[22] & 127 or frame[23]:
        return None
    source, word = struct.unpack_from('!HH', frame, 24)
    port, length = (source >> 6) & 63, word & 0x3fff
    if source >> 12 or port not in ports or not 14 <= length <= MAX_FRAME or len(frame) < 32+length:
        return None
    return port, frame[32:32+length]


def encode(port, frame):
    if type(port) is not int or port not in FRONT or not 14 <= len(frame) <= MAX_FRAME:
        raise ValueError('invalid front port or Ethernet length')
    # Requires a commissioned Jericho injected-header/RAW_DSA trunk.
    return b'\x01'+struct.pack('!H', FRONT[port])+b'\0'+frame[:12]+bytes(8)+frame[12:]


def decode_otmh_ssp(frame, ports):
    """Commissioned BCM24 TM_SSP return, TC0, front ingress RAW.

    Four-byte OTMH: observed destination24 then source system port, both BE16.
    BCM and PKI remove link FCS; the remaining bytes are the Ethernet frame.
    Never infer a source
    port from the payload, or accept an untagged return with no source metadata.
    """
    if not 18 <= len(frame) <= MAX_FRAME+4 or frame[:2]!=b'\0\x18':
        return None
    source=struct.unpack_from('!H',frame,2)[0]
    reverse={v:k for k,v in FRONT.items()}
    port=reverse.get(source)
    if port not in ports:
        return None
    return port,frame[4:]


RX_FORMATS={'fe100-sysport':decode,'bcm-otmh-ssp':decode_otmh_ssp}


def validate_trunk(name, root=Path('/sys/class/net')):
    if not name or '/' in name or name in ('.','..'):
        raise ValueError('invalid trunk name')
    path = root/name
    if not (path/'device').exists():
        raise ValueError('trunk must be a physical netdevice')
    if (int((path/'type').read_text()) != 1 or int((path/'mtu').read_text()) < MAX_FRAME+32
            or not int((path/'flags').read_text(),16) & 1):
        raise ValueError('trunk must be Ethernet, enabled, with MTU >= 1550')


def pump(rx, tx, taps, inspector, seconds=0, counters=None, decoder=decode):
    counts = counters if counters is not None else collections.Counter()
    byfd = {fd: port for port, fd in taps.items()}
    deadline = time.monotonic()+seconds if seconds else float('inf')
    rx.setblocking(False); tx.setblocking(False)
    while time.monotonic() < deadline:
        inspector.tick()
        ready, _, _ = select.select([rx, *byfd], [], [], .25)
        # One bounded datagram per ready descriptor: no unbounded queue and
        # no retry after an ambiguous transmit. Count pressure explicitly.
        for source in ready:
            try:
                if source is rx:
                    frame, addr = rx.recvfrom(65536)
                    if addr[2] == socket.PACKET_OUTGOING:
                        counts['outgoing_ignored'] += 1; continue
                    item = decoder(frame, taps)
                    if item is None:
                        counts['envelope_rejected'] += 1; continue
                    port, payload = item
                    if not inspector.allow(port, payload):
                        counts['inspection_drop'] += 1; continue
                    if os.write(taps[port], payload) != len(payload):
                        raise OSError('short TAP packet write')
                    counts['rx_p%d' % port] += 1
                else:
                    payload = os.read(source, MAX_FRAME+1)
                    if not payload:
                        raise RuntimeError('TAP closed')
                    port = byfd[source]
                    if not 14 <= len(payload) <= MAX_FRAME:
                        counts['length_drop'] += 1; continue
                    packet = encode(port, payload)
                    if tx.send(packet) != len(packet):
                        raise OSError('short trunk packet write')
                    counts['tx_p%d' % port] += 1
            except BlockingIOError:
                counts['backpressure_drop'] += 1
    return dict(counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rx', required=True)
    parser.add_argument('--tx', required=True)
    parser.add_argument('--ports', required=True, help='commissioned front ports, comma separated')
    parser.add_argument('--rx-format',choices=tuple(RX_FORMATS),default='fe100-sysport')
    parser.add_argument('--seconds', type=int, default=0)
    args = parser.parse_args()
    ports = [int(v) for v in args.ports.split(',')]
    if not ports or len(set(ports)) != len(ports) or set(ports)-FRONT.keys() or not 0 <= args.seconds <= 3600:
        parser.error('invalid ports or duration')
    # A DP-only entry point, never a generic-platform auto-probe.
    cpu = Path('/proc/cpuinfo').read_text()
    if 'Octeon' not in cpu and 'OCTEON' not in cpu:
        raise RuntimeError('OCTEON dataplane required')
    if sum(line.startswith('processor') for line in cpu.splitlines()) < 16:
        raise RuntimeError('dataplane CPU topology required')
    from ffn_dp_boot_health import inspect_boot
    if not inspect_boot()['ready']:
        raise RuntimeError('DP Debian boot is incomplete')
    validate_trunk(args.rx); validate_trunk(args.tx)
    from ffn_inspection import Inspector
    handles = {}; sockets = []; inspector = None
    counts = collections.Counter()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    with open('/run/ffn-fabric.lock','a') as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with open('/run/ffn-network.lock','a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                links = {p['ifname']:p for p in json.loads(run('ip','-n','ffn-data','-j','link'))}
                for port in ports:
                    item = links.get('p%d' % port)
                    if not item or item['mtu'] > 1500:
                        raise RuntimeError('existing front TAP with MTU <= 1500 required')
                    handles[port] = tap(port)
            for name in (args.rx,args.tx):
                sock = socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3))
                sockets.append(sock); sock.bind((name,0))
            sockets[0].setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,4*1024*1024)
            inspector = Inspector()
            pump(*sockets, handles, inspector, args.seconds, counts,RX_FORMATS[args.rx_format])
        except KeyboardInterrupt:
            pass
        finally:
            if inspector: inspector.close()
            for sock in sockets: sock.close()
            for fd in handles.values(): os.close(fd)
            print(json.dumps({'transport':'direct-dp','rx_format':args.rx_format,
                              'hardware_offload':False,'counters':dict(counts)}))


if __name__ == '__main__': main()
