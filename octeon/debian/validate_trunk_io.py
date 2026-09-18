#!/usr/bin/env python3
"""Bounded raw-trunk commissioning: real PKO completion and nonce-matched RX.

Does not reconfigure ports, bridges or routes. Requires explicitly named,
already connected test ports. Exit2 means packet round-trip was not proven.
"""
import argparse
import fcntl
import json
from pathlib import Path
import select
import socket
import time
import uuid
from ffn_dp_packet_transport import FRONT, RX_FORMATS, encode, validate_trunk


def probe_mapping(ports,profile=None):
    """Use explicit board wiring while commissioning; never certify it here."""
    mapping=dict(FRONT)
    if profile is not None:
        from ffn_copper_vif import validate_profile
        validated=validate_profile(profile)
        for port in ports:
            if port in range(1,5):
                row=validated.get(str(port))
                if row is None:raise ValueError('selected copper port lacks a physical mapping')
                mapping[port]=row['bcm_port']
    if not ports or len(set(ports))!=len(ports) or any(type(p) is not int or p not in mapping for p in ports):
        raise ValueError('invalid front ports')
    return mapping


def probe_frame(port, sequence, size, token, vlan=None):
    header=bytes.fromhex('02ff0000000202ff00000001')
    if vlan is not None:
        if type(vlan) is not int or not 1 <= vlan <= 4094:
            raise ValueError('VLAN must be 1..4094')
        header+=b'\x81\x00'+vlan.to_bytes(2,'big')
    payload=header+b'\x88\xb5'+token+bytes([port,sequence])
    if not len(payload) <= size <= 1518:
        raise ValueError('invalid probe frame length')
    return payload+bytes((i+sequence)%256 for i in range(size-len(payload)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ports', required=True)
    parser.add_argument('--seconds', type=float, default=5)
    parser.add_argument('--count', type=int, default=1, help='frames per port, paced at 100 frames/s')
    parser.add_argument('--size', type=int, default=64, help='Ethernet frame length without FCS')
    parser.add_argument('--pairs', help='expected return ports, e.g. 5:13,13:5')
    parser.add_argument('--rx-format',choices=tuple(RX_FORMATS),default='fe100-sysport')
    parser.add_argument('--copper-profile',type=Path,help='explicit physical wiring profile for copper commissioning')
    parser.add_argument('--vlan',type=int,help='include an 802.1Q tag (1..4094); size includes the tag')
    args = parser.parse_args()
    ports = [int(x) for x in args.ports.split(',')]
    try:front=probe_mapping(ports,json.loads(args.copper_profile.read_text()) if args.copper_profile else None)
    except (ValueError,OSError) as exc:parser.error(str(exc))
    if not 1 <= args.seconds <= 30:
        parser.error('seconds must be 1..30')
    if not 1 <= args.count <= 64 or not 64 <= args.size <= 1518:
        parser.error('count must be 1..64 and size 64..1518')
    if args.vlan is not None and not 1 <= args.vlan <= 4094:
        parser.error('VLAN must be 1..4094')
    peers = {}
    if args.pairs:
        try:
            pairs = [tuple(map(int,p.split(':'))) for p in args.pairs.split(',')]
            peers = dict(pairs)
            if len(peers) != len(pairs) or set(peers) != set(ports) or set(peers.values()) != set(ports):
                raise ValueError()
        except ValueError:
            parser.error('pairs must map every selected port exactly once')
    validate_trunk('ffnpkt0')
    status = lambda: json.loads(Path('/sys/kernel/debug/ffn_dp_packet_init/status').read_text())
    token = b'FFN-TRUNK-TEST:' + uuid.uuid4().bytes
    received = []
    other = 0
    samples = []
    expected = {}
    with open('/run/ffn-fabric.lock','a') as owner,socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3)) as sock:
        fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
        before = status()
        sock.bind(('ffnpkt0',0))
        sock.setblocking(False)
        for sequence in range(args.count):
            for port in ports:
                # Experimental ethertype, locally administered unicast MACs.
                payload = probe_frame(port,sequence,args.size,token,args.vlan)
                expected[port,sequence]=payload
                packet = encode(port,payload,front)
                if sock.send(packet) != len(packet):
                    raise RuntimeError('short packet submission')
                time.sleep(.01)
        deadline = time.monotonic()+args.seconds
        while time.monotonic() < deadline:
            if not select.select([sock],[],[],min(.1,max(0,deadline-time.monotonic())))[0]:
                continue
            data, addr = sock.recvfrom(65536)
            if addr[2] == socket.PACKET_OUTGOING:
                continue
            if token in data and len(samples)<8:
                samples.append({'length':len(data),'prefix_hex':data[:256].hex()})
            result = RX_FORMATS[args.rx_format](data, ports,front) if args.rx_format=='bcm-otmh-ssp' else RX_FORMATS[args.rx_format](data,ports)
            if result and token in result[1]:
                offset = result[1].index(token)+len(token)
                if offset+1 < len(result[1]):
                    sent_port, sequence=result[1][offset:offset+2]
                    if expected.get((sent_port,sequence))==result[1] and (not peers or peers[sent_port]==result[0]):
                        received.append({'sent_port':sent_port, 'received_port':result[0], 'sequence':sequence})
            else:
                other += 1
        after = status()
    b, a = before['trunk'], after['trunk']
    result = {'schema':1,'rx_format':args.rx_format,'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
              'ports':ports,'physical_mapping':{p:front[p] for p in ports},'expected_peers':peers,'frame_size':args.size,'vlan':args.vlan,'sent':len(expected),
              'matched_rx':received,'other_rx':other,'raw_rx_samples':samples,
              'tx_accepted_delta':a['tx_accepted']-b['tx_accepted'],
              'tx_completed_delta':a['tx_completed']-b['tx_completed'],
              'wire_tx_delta':a.get('wire_tx',0)-b.get('wire_tx',0),
              'wire_rx_delta':a.get('wire_rx',0)-b.get('wire_rx',0),
              'rx_delta':a['rx']-b['rx'],'before':before,'after':after,
              'session_offload_verified':False}
    result['round_trip_verified'] = (not a['error'] and not a['bad_dma'] and
        result['tx_completed_delta'] >= len(expected) and
        result['wire_tx_delta'] >= len(expected) and
        set((x['sent_port'],x['sequence']) for x in received) == set(expected))
    print(json.dumps(result,indent=2))
    raise SystemExit(0 if result['round_trip_verified'] else 2)


if __name__ == '__main__': main()
