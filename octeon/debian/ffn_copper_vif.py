#!/usr/bin/env python3
"""Copper VIF driver: commissioned wire mapping and expiring PHY/MAC carrier.

The MP supplies observations through the DP owner's root-only control socket.
This adapter never writes PHY registers, provisions queues, or infers wiring.
"""
import json
from pathlib import Path
import time
import uuid

PROFILE = Path('/etc/ffn/vif-copper.json')
# A CP inventory takes 6-8 seconds on OCTEON. Cover two poll cycles while
# retaining an absolute, non-renewable deadline from challenge issuance.
LEASE_SECONDS = 30


def validate_profile(value):
    if not isinstance(value, dict) or set(value) != {'version', 'ports'} or type(value['version']) is not int or value['version'] != 1:
        raise ValueError('invalid copper VIF profile')
    ports = value['ports']
    if not isinstance(ports, dict) or set(ports) - {'1', '2', '3', '4'}:
        raise ValueError('invalid copper VIF ports')
    phys, macs = set(), set()
    for entry in ports.values():
        if not isinstance(entry, dict) or set(entry) != {'phy', 'bcm_port', 'packet_path_verified'}:
            raise ValueError('invalid copper VIF mapping')
        phy, mac = entry['phy'], entry['bcm_port']
        if (type(phy) is not int or phy not in range(16, 20) or phy in phys
                or type(mac) is not int or mac not in (28, 13, 14, 15) or mac in macs
                or type(entry['packet_path_verified']) is not bool):
            raise ValueError('duplicate or invalid copper PHY/MAC mapping')
        phys.add(phy); macs.add(mac)
    return json.loads(json.dumps(ports))


class CopperVif:
    def __init__(self, profile=None, clock=time.monotonic):
        if profile is None:
            profile = json.loads(PROFILE.read_text()) if PROFILE.exists() else {'version': 1, 'ports': {}}
        self.mapping = validate_profile(profile)
        self.clock = clock
        self.pending = {}
        self.last_sample = -1
        self.expires = 0
        self.rows = {}

    @property
    def ports(self):
        return {int(p) for p, entry in self.mapping.items() if entry['packet_path_verified']}

    def wire_map(self, optical):
        # Remove every copper default first: a commissioned BCM28 must never
        # alias both physical port1 and port2 in the ingress reverse map.
        return {p: mac for p, mac in optical.items() if p > 4} | {
            int(p): entry['bcm_port'] for p, entry in self.mapping.items() if entry['packet_path_verified']}

    def challenge(self):
        now = self.clock()
        self.pending = {k: v for k, v in self.pending.items() if now - v < LEASE_SECONDS}
        if len(self.pending) >= 16:
            del self.pending[next(iter(self.pending))]
        token = str(uuid.uuid4())
        self.pending[token] = now
        return token

    def observe(self, payload):
        if not isinstance(payload, dict) or set(payload) != {'token', 'ports'} or not isinstance(payload['token'], str):
            raise ValueError('invalid copper observation')
        issued = self.pending.pop(payload['token'], None)
        if issued is None or not 0 <= self.clock() - issued < LEASE_SECONDS or issued <= self.last_sample:
            raise ValueError('expired or superseded copper observation')
        rows = payload['ports']
        if not isinstance(rows, list) or len(rows) > 24:
            raise ValueError('invalid copper observation ports')
        parsed = {}
        for row in rows:
            if not isinstance(row, dict) or type(row.get('port')) is not int or row['port'] not in range(1, 25) or row['port'] in parsed:
                raise ValueError('invalid or duplicate observed port')
            parsed[row['port']] = dict(row)
        self.rows, self.last_sample, self.expires = parsed, issued, issued + LEASE_SECONDS

    def status(self, port):
        entry = self.mapping.get(str(port))
        result = {'carrier': False, 'speed_mbps': None, 'phy': None, 'bcm_port': None,
                  'packet_path_verified': False, 'reason': 'physical mapping unverified'}
        if entry is None:
            return result
        result.update(entry)
        row = self.rows.get(port, {})
        if self.clock() >= self.expires:
            reason = 'PHY/MAC observation missing or stale'
        elif (row.get('phy_mapping_verified') is not True or type(row.get('phy_address')) is not int
                or row.get('phy_address') != entry['phy'] or type(row.get('bcm_port')) is not int
                or row.get('bcm_port') != entry['bcm_port']):
            reason = 'PHY/MAC mapping changed or unavailable'
        elif row.get('phy_pending') is not False:
            reason = 'PHY configuration pending or unknown'
        elif not all(row.get(k) is True for k in ('available', 'enabled', 'phy_enabled', 'mac_enabled')):
            reason = 'PHY or MAC administratively disabled or unavailable'
        elif not all(row.get(k) is True for k in ('link', 'mac_link', 'datapath_link')):
            reason = 'PHY or MAC link down'
        elif (type(row.get('speed_mbps')) is not int or row['speed_mbps'] not in (100, 1000, 10000)
                or type(row.get('mac_speed_mbps')) is not int or row['speed_mbps'] != row['mac_speed_mbps']):
            reason = 'PHY and MAC speeds are not synchronized'
        elif row.get('packet_path_ready') is not True:
            reason = 'BCM packet path unavailable or unqualified'
        else:
            result['speed_mbps'] = row['speed_mbps']
            reason = 'ready' if entry['packet_path_verified'] else 'packet path not commissioned'
        result.update(carrier=reason == 'ready', reason=reason)
        return result

    def allowed(self, port):
        return port > 4 or self.status(port)['carrier']

    def inventory(self):
        return {str(p): self.status(p) for p in range(1, 5)}
