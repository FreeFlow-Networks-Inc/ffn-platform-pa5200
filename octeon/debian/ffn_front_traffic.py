#!/usr/bin/env python3
"""Read front-panel packet counters through the existing BCM owner only."""
import json
from pathlib import Path
import socket
import time


def normalize(reply, links):
    if not reply.get('ok') or reply.get('truncated'):
        raise ValueError('BCM counters unavailable or incomplete')
    # show c reports packet counters, not octets. Never invent byte rates.
    headers = ' '.join(reply.get('sample', []))
    if 'snmpEtherStatsRXNoErrors' not in headers:
        raise ValueError('Unrecognized counter table')
    values = {}
    for row in reply.get('counters', []):
        if row['counter'] in ('snmpEtherStatsRXNoErrors', 'snmpEtherStatsTXNoErrors'):
            value = row['value']
            if type(value) is not int or value < 0: raise ValueError('Invalid counter')
            values[(str(row['port']), row['dir'])] = value
    ports = []
    for link in links['ports']:
        port = link['port']
        if type(port) is not int or not 1 <= port <= 24: continue
        valid = links.get('stale') is False and not links.get('error')
        ports.append(dict(name='ethernet1/%d' % port, port=port, type='data',
            link=link.get('carrier') if valid else None,
            speed_gbps=(link.get('speed_mbps') or 0)/1000 if valid else None,
            rx_packets_total=values.get((str(link['bcm_port']), 'RX'), 0),
            tx_packets_total=values.get((str(link['bcm_port']), 'TX'), 0)))
    if len(ports) != 24 or len({p['name'] for p in ports}) != 24:
        raise ValueError('Incomplete front-port map')
    return dict(ports=ports, unit='pps', source='BCM front-panel packet counters',
                byte_rates_available=False, boot_id=links['boot_id'], sample_monotonic=time.monotonic())


def sample():
    from ffn_port_events import STATE, qualify
    with socket.create_connection(('127.1.1.2',8104), timeout=5) as sock:
        sock.settimeout(10); sock.sendall(b'{"op":"port.counters"}\n')
        data = bytearray()
        while b'\n' not in data and len(data) <= 1048576:
            part = sock.recv(65536)
            if not part: break
            data.extend(part)
        if not data.endswith(b'\n') or len(data) > 1048576: raise ValueError('Incomplete counters')
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    links = qualify(json.loads(STATE.read_text()), boot, time.monotonic())
    return normalize(json.loads(data), links)


if __name__ == '__main__': print(json.dumps(sample()))
