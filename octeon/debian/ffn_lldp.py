#!/usr/bin/env python3
"""Dataplane plane verb for the LLDP agent (ffn-lldpd): status, validate, apply.

The MP applier compiles the committed LLDP intent (global enable, per-port
enable and profile, system identity) and applies it here with the usual
revision fence. This verb resolves each port's frame source address from the
dataplane namespace, writes /etc/ffn/lldp.json atomically and reloads the
agent; status returns the applied configuration together with the agent's
published counters and neighbour table.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

INTENT = Path('/etc/ffn/lldp.json')
RUN = Path('/run/ffn-lldp')
NAMESPACE = 'ffn-data'
UNIT = 'ffn-lldpd.service'
FRONT = dict(enumerate((16, 1, 18, 19, 6, 21, 22, 23, 7, 11, 36, 27, 10, 29, 30, 31, 32, 33, 34, 35), 5))
FRONT.update({1: 28, 2: 13, 3: 14, 4: 15})
MODES = ('transmit-receive', 'transmit-only', 'receive-only')
TLVS = ('port-description', 'system-name', 'system-description', 'system-capabilities', 'management-address')
FIELDS = {'revision', 'lldp', 'configuration'}


def revision_of(config):
    return int(hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:13], 16)


def load():
    return json.loads(INTENT.read_text()) if INTENT.exists() else {}


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    with temp.open('w') as out:
        json.dump(value, out)
        out.flush()
        os.fsync(out.fileno())
    temp.replace(path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def link_addresses(run=subprocess.run):
    """MAC per dataplane netdev (pN and aeN), from the forwarding namespace."""
    result = run(['ip', '-n', NAMESPACE, '-j', 'link', 'show'], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError('dataplane links unavailable')
    return {row['ifname']: row.get('address', '') for row in json.loads(result.stdout or '[]')}


def validate_intent(value):
    """The committed intent shape; raises ValueError with a precise reason."""
    if not isinstance(value, dict) or set(value) - {'enabled', 'system', 'interval', 'hold', 'ports'}:
        raise ValueError('lldp intent must carry enabled, system, interval, hold and ports')
    enabled = value.get('enabled', False)
    if type(enabled) is not bool:
        raise ValueError('enabled must be boolean')
    system = value.get('system') or {}
    if not isinstance(system, dict) or set(system) - {'chassis_id', 'name', 'description'}:
        raise ValueError('system carries chassis_id, name and description')
    chassis = str(system.get('chassis_id', ''))
    parts = chassis.split(':')
    if len(parts) != 6 or any(len(p) != 2 or any(c not in '0123456789abcdef' for c in p) for p in parts):
        raise ValueError('system.chassis_id must be a lowercase MAC address')
    interval, hold = value.get('interval', 30), value.get('hold', 4)
    if type(interval) is not int or not 5 <= interval <= 32768 or type(hold) is not int or not 2 <= hold <= 10:
        raise ValueError('interval 5..32768 and hold 2..10 required')
    ports = value.get('ports', [])
    if not isinstance(ports, list) or len(ports) > 24:
        raise ValueError('ports must be a list of at most 24 entries')
    seen = set()
    for port in ports:
        if not isinstance(port, dict) or set(port) - {'port', 'mode', 'tlvs', 'description', 'management', 'aggregate'}:
            raise ValueError('port entries carry port, mode, tlvs, description, management and aggregate')
        number = port.get('port')
        if type(number) is not int or number not in FRONT or number in seen:
            raise ValueError('port must be a distinct faceplate port 1..24')
        seen.add(number)
        if port.get('mode', 'transmit-receive') not in MODES:
            raise ValueError('port %d: mode must be one of %s' % (number, ', '.join(MODES)))
        tlvs = port.get('tlvs', list(TLVS))
        if not isinstance(tlvs, list) or any(t not in TLVS for t in tlvs):
            raise ValueError('port %d: unknown option TLV' % number)
        if not isinstance(port.get('description', ''), str) or len(port.get('description', '')) > 255:
            raise ValueError('port %d: description too long' % number)
        management = port.get('management', [])
        if not isinstance(management, list) or len(management) > 8 or any(not isinstance(a, str) for a in management):
            raise ValueError('port %d: management must be a list of addresses' % number)
        aggregate = port.get('aggregate', '')
        if not isinstance(aggregate, str) or (aggregate and not aggregate.startswith('ae')):
            raise ValueError('port %d: aggregate must name an aeN interface' % number)
    return dict(enabled=enabled, system=dict(chassis_id=chassis, name=str(system.get('name', ''))[:255],
                                                description=str(system.get('description', ''))[:255]),
                interval=interval, hold=hold,
                ports=sorted((dict(port=p['port'], mode=p.get('mode', 'transmit-receive'), tlvs=list(p.get('tlvs', list(TLVS))),
                                   description=p.get('description', ''), management=list(p.get('management', [])),
                                   aggregate=p.get('aggregate', '')) for p in ports), key=lambda p: p['port']))


def render(intent, configuration, addresses):
    """The agent's document: one entry per port with its frame source address."""
    ports = []
    if intent.get('enabled'):
        for p in intent['ports']:
            netdev = p['aggregate'] or 'p%d' % p['port']
            mac = addresses.get(netdev)
            if not mac:
                raise ValueError('ethernet1/%d: dataplane device %s has no address' % (p['port'], netdev))
            ports.append(dict(name='ethernet1/%d' % p['port'], source=FRONT[p['port']], mac=mac, mode=p['mode'], tlvs=p['tlvs'],
                              description=p['description'], management=p['management']))
    return dict(revision=str(revision_of(intent)), configuration=configuration, interval=intent.get('interval', 30),
                hold=intent.get('hold', 4), system=intent.get('system', {}), ports=ports)


def published(name):
    try:
        return json.loads((RUN / name).read_text())
    except (OSError, ValueError):
        return None


def status():
    saved = load()
    intent = saved.get('intent', {})
    return dict(config=dict(revision=revision_of(intent) if intent else 0, lldp=intent, configuration=saved.get('configuration')),
                agent=published('status.json'), neighbours=published('neighbors.json'))


def execute(action, payload, run=subprocess.run):
    if action == 'status':
        if payload:
            raise ValueError('status takes no payload')
        return status()
    if action not in ('validate', 'apply') or not isinstance(payload, dict) or set(payload) != FIELDS:
        raise ValueError('validate/apply take revision, lldp and configuration')
    current = status()['config']
    if type(payload['revision']) is not int or payload['revision'] != current['revision']:
        raise ValueError('lldp revision changed; refresh before applying')
    intent = validate_intent(payload['lldp'])
    if not isinstance(payload['configuration'], str) or not payload['configuration']:
        raise ValueError('committed configuration digest required')
    document = render(intent, payload['configuration'], link_addresses(run) if intent.get('enabled') and intent['ports'] else {})
    if action == 'validate':
        return dict(validated=True, ports=len(document['ports']))
    atomic(INTENT, dict(document, intent=intent))
    run(['systemctl', 'reload-or-restart', UNIT], check=True, capture_output=True, text=True, timeout=30)
    return dict(applied=True, **status())


if __name__ == '__main__':
    try:
        payload = json.load(sys.stdin) if sys.argv[1] != 'status' else (json.load(sys.stdin) if not sys.stdin.isatty() else {})
        print(json.dumps(execute(sys.argv[1], payload)))
    except (ValueError, KeyError, IndexError) as error:
        print(json.dumps({'error': str(error)}))
        sys.exit(2)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({'error': str(error)}))
        sys.exit(1)
