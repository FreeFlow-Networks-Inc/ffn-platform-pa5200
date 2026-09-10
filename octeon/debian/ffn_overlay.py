#!/usr/bin/env python3
"""DP overlay/MACsec interface controller, isolated in ffn-data.

MACsec device provisioning is separate from association/key management.
No keys are accepted, persisted or returned by this controller.
"""
import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess as S
import sys

STATE = Path('/etc/ffn/overlay.json')
NETWORK = Path('/etc/ffn/network.json')
NS = 'ffn-data'
OVERHEAD = {'vxlan': 50, 'geneve': 50, 'gretap': 42, 'gre': 28, 'ipip': 20, 'macsec': 32}
EMPTY = {'revision': 0, 'links': {}}


def run(*args):
    p = S.run(args, text=True, capture_output=True)
    if p.returncode:
        raise RuntimeError('%s: %s' % (' '.join(args), p.stderr.strip()))
    return p.stdout


def ip(*args):
    return run('ip', '-n', NS, *args)


def load():
    return json.loads(STATE.read_text()) if STATE.exists() else dict(EMPTY)


def validate(cfg, network):
    if not isinstance(cfg, dict) or set(cfg) != {'revision', 'links'} or type(cfg['revision']) is not int or cfg['revision'] < 0:
        raise ValueError('expected nonnegative revision and links')
    if not isinstance(cfg['links'], dict) or len(cfg['links']) > 32:
        raise ValueError('links must be an object with at most 32 interfaces')
    addresses = {str(ipaddress.ip_interface(a).ip) for p in network['ports'].values()
                 for a in p.get('addresses', [])}
    for name, c in cfg['links'].items():
        if not re.fullmatch(r'ov[a-z0-9]{1,12}', name):
            raise ValueError('managed names must be ov followed by 1..12 lowercase letters/digits')
        if not isinstance(c, dict) or set(c)-{'kind', 'underlay', 'local', 'remote', 'vni', 'key', 'mtu', 'addresses', 'dstport', 'replay_window'}:
            raise ValueError('unknown link setting')
        kind = c.get('kind')
        if kind not in OVERHEAD or c.get('underlay') not in network['ports']:
            raise ValueError('unsupported kind or unknown underlay')
        underlay = network['ports'][c['underlay']]
        if underlay['mode'] != 'l3':
            raise ValueError('underlay must be an enabled routed port; remove bridge membership first')
        mtu = c.get('mtu', 1400)
        if type(mtu) is not int or not 1280 <= mtu <= min(1500, underlay.get('mtu', 1500))-OVERHEAD[kind]:
            raise ValueError('MTU exceeds supported underlay encapsulation budget')
        if kind == 'macsec':
            if type(c.get('replay_window', 64)) is not int or not 0 <= c.get('replay_window', 64) <= 0xffffffff:
                raise ValueError('invalid MACsec replay window')
            if set(c) & {'local', 'remote', 'vni', 'key', 'dstport'}:
                raise ValueError('MACsec association keys/endpoints are managed separately')
        else:
            if 'replay_window' in c:
                raise ValueError('replay window only applies to MACsec')
            for field in ('local', 'remote'):
                address = ipaddress.IPv4Address(c[field])
                if address.is_multicast or address.is_unspecified or address.is_loopback:
                    raise ValueError('tunnel endpoints must be unicast IPv4 addresses')
            if c['local'] == c['remote']:
                raise ValueError('local and remote endpoints must differ')
            if c['local'] not in [str(ipaddress.ip_interface(a).ip) for a in underlay.get('addresses', [])]:
                raise ValueError('local endpoint must already be assigned to underlay')
            if kind in ('vxlan', 'geneve'):
                if type(c.get('vni')) is not int or not 1 <= c['vni'] <= 16777215 or 'key' in c:
                    raise ValueError('UDP overlays require VNI 1..16777215 and no GRE key')
                if type(c.get('dstport', 4789)) is not int or not 1 <= c.get('dstport', 4789) <= 65535:
                    raise ValueError('invalid UDP destination port')
            elif set(c) & {'vni', 'dstport'}:
                raise ValueError('VNI/UDP port only apply to VXLAN/Geneve')
            if 'key' in c and (kind not in ('gre', 'gretap') or type(c['key']) is not int or not 0 <= c['key'] <= 0xffffffff):
                raise ValueError('key must be a 32-bit GRE key')
        if not isinstance(c.get('addresses', []), list):
            raise ValueError('addresses must be a list')
        for a in c.get('addresses', []):
            addr = ipaddress.ip_interface(a)
            if addr.ip.is_multicast or addr.ip.is_unspecified or addr.ip.is_loopback or str(addr.ip) in addresses:
                raise ValueError('invalid or duplicate overlay address')
            addresses.add(str(addr.ip))
    return cfg


def create_args(name, c):
    kind = c['kind']
    args = ['link', 'add', 'link', c['underlay'], 'name', name, 'type', kind]
    if kind == 'macsec':
        args += ['port', '1', 'encrypt', 'on', 'protect', 'on', 'validate', 'strict', 'replay', 'on', 'window', str(c.get('replay_window', 64))]
    else:
        # Geneve's fixed remote endpoint uses routing to select its underlay.
        if kind == 'geneve':
            args = ['link', 'add', 'name', name, 'type', kind]
        args += ['remote', c['remote']]
        if kind != 'geneve':
            args += ['local', c['local'], 'dev', c['underlay']]
        if kind in ('vxlan', 'geneve'):
            args += ['id', str(c['vni']), 'dstport', str(c.get('dstport', 4789 if kind == 'vxlan' else 6081))]
            if kind == 'vxlan':
                args += ['nolearning']
        elif 'key' in c:
            args += ['key', str(c['key'])]
    return args


def check_ownership(names):
    live = {x['ifname']: x for x in json.loads(ip('-j', '-d', 'link', 'show'))}
    for name in names:
        if name in live and live[name].get('ifalias') != 'ffn-overlay:'+name:
            raise ValueError('refusing to replace unowned interface '+name)
    return live


def remove(name):
    live = check_ownership([name])
    if name in live:
        ip('link', 'delete', name)


def create(name, c):
    if c['kind'] == 'geneve':
        route = json.loads(ip('-j', 'route', 'get', c['remote']))[0]
        if route.get('dev') != c['underlay'] or route.get('prefsrc') != c['local']:
            raise ValueError('Geneve route must select requested underlay and source')
    ip(*create_args(name, c))
    # Tag ownership immediately so rollback cannot remove an unrelated link.
    ip('link', 'set', name, 'alias', 'ffn-overlay:'+name)
    ip('link', 'set', name, 'mtu', str(c.get('mtu', 1400)))
    for address in c.get('addresses', []):
        ip('address', 'add', address, 'dev', name)
    ip('link', 'set', name, 'up')


def save(cfg):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE.with_suffix('.tmp')
    with temp.open('w') as f:
        json.dump(cfg, f, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    temp.replace(STATE)


def apply(old, new):
    changed = [n for n in old['links'].keys() | new['links'].keys() if old['links'].get(n) != new['links'].get(n)]
    check_ownership(changed)
    applied = []
    try:
        for name in changed:
            applied.append(name)
            remove(name)
            if name in new['links']:
                create(name, new['links'][name])
        save(new)
    except BaseException as error:
        failures = []
        for name in reversed(applied):
            try:
                remove(name)
                if name in old['links']:
                    create(name, old['links'][name])
            except Exception as rollback:
                failures.append(str(rollback))
        raise RuntimeError('overlay update failed: %s; rollback errors: %s' % (error, failures)) from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('status', 'set', 'apply'))
    args = parser.parse_args()
    with open('/run/ffn-network.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cfg = load()
        if args.action == 'status':
            live = json.loads(ip('-j', '-d', 'address', 'show'))
            print(json.dumps({'config': cfg, 'interfaces': [i for i in live if i['ifname'] in cfg['links']],
                              'offload': 'software encapsulation; no NIC tunnel/MACsec offload verified; Crypto API may use OCTEON AES',
                              'macsec_keys': 'not managed by this controller'}))
        elif args.action == 'set':
            new = validate(json.load(sys.stdin), json.loads(NETWORK.read_text()))
            if new['revision'] != cfg['revision']:
                raise ValueError('revision conflict')
            new['revision'] += 1
            apply(cfg, new)
            print(json.dumps(new))
        else:
            validate(cfg, json.loads(NETWORK.read_text()))
            # Boot replay only; protect live state from accidental recreation.
            live = check_ownership(cfg['links'])
            for name, c in cfg['links'].items():
                if name not in live:
                    create(name, c)
            print(json.dumps({'applied': list(cfg['links'])}))


if __name__ == '__main__':
    main()
