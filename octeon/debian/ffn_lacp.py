#!/usr/bin/env python3
"""PA-5220 LACP profiles and guarded Linux 802.3ad bond lifecycle.

No members are assigned by installation. Profiles can be saved on the current
relay; activation requires an explicitly qualified physical LACP backend.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import ffn_network as net

STATE = Path('/etc/ffn/lacp.json')


def validate(cfg):
    if (not isinstance(cfg, dict) or set(cfg) != {'revision', 'groups'} or
            type(cfg['revision']) is not int or cfg['revision'] < 0 or
            not isinstance(cfg['groups'], dict) or len(cfg['groups']) > 12):
        raise ValueError('expected revision and at most 12 groups')
    used = set()
    for name, g in cfg['groups'].items():
        if not re.fullmatch(r'lag(?:[1-9]|1[0-2])', name):
            raise ValueError('group name must be lag1..lag12')
        fields = {'members', 'activity', 'rate', 'hash', 'min_links', 'system_priority', 'network'}
        if not isinstance(g, dict) or set(g) != fields:
            raise ValueError('group requires members, activity, rate, hash, min_links, system_priority and network')
        members = g['members']
        if (not isinstance(members, list) or not 2 <= len(members) <= 8 or
                any(not isinstance(p, str) or not re.fullmatch(r'p(?:[1-9]|1[0-9]|2[0-4])', p) for p in members)):
            raise ValueError('group requires 2..8 front ports p1..p24')
        if len(set(members)) != len(members) or used.intersection(members):
            raise ValueError('a member may belong to only one LACP group')
        used.update(members)
        if g['activity'] not in ('active', 'passive') or g['rate'] not in ('fast', 'slow'):
            raise ValueError('invalid LACP activity or rate')
        if g['hash'] not in ('layer2', 'layer2+3'):
            raise ValueError('hash must be layer2 or layer2+3')
        if type(g['min_links']) is not int or not 1 <= g['min_links'] <= len(members):
            raise ValueError('min_links must be 1..member count')
        if type(g['system_priority']) is not int or not 1 <= g['system_priority'] <= 65535:
            raise ValueError('system_priority must be 1..65535')
        net.validate_port('p1', g['network'])
        if g['network']['mode'] == 'disabled':
            raise ValueError('group network mode must be l2 or l3; use deactivate to stop a group')
    return cfg


def load():
    return validate(json.loads(STATE.read_text())) if STATE.exists() else {'revision': 0, 'groups': {}}


def save(cfg):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix('.tmp')
    with tmp.open('w') as f:
        json.dump(cfg, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(STATE)


def capability():
    backend = net.backend()
    supported = backend.get('lacp_qualified') is True
    return {'activation_supported': supported,
            'reason': '' if supported else 'Physical carrier, speed/duplex and LACPDU transport are not qualified on this backend',
            'backend': backend.get('transport'), 'hardware_offload': False}


def runtime():
    if not net.exists():
        return {}
    result = {}
    for link in json.loads(net.ip('-j', '-d', 'link', 'show', 'type', 'bond')):
        # Some iproute2 builds emit empty objects for filtered-out devices.
        name = link.get('ifname', '')
        if re.fullmatch(r'lag(?:[1-9]|1[0-2])', name) and link.get('ifalias') == 'ffn-lacp:' + name:
            members = json.loads(net.ip('-j', '-d', 'link', 'show', 'master', name))
            result[name] = {'link': link, 'members': members,
                            'bonding': net.run('ip', 'netns', 'exec', net.NS,
                                              'cat', '/proc/net/bonding/' + name)}
    return result


def preflight(name, g):
    if not capability()['activation_supported']:
        raise ValueError(capability()['reason'])
    cfg = net.validate(json.loads(net.STATE.read_text()))
    # Validate addresses/VRF against current port configuration without making
    # the standalone controller an alternate route or policy manager.
    probe = json.loads(json.dumps(cfg))
    for p in g['members']:
        if cfg['ports'].get(p, {}).get('mode') != 'disabled':
            raise ValueError('disable member ports in network controls before activation')
    probe['ports'][g['members'][0]] = g['network']
    net.validate(probe)
    links = {p['ifname']: p for p in json.loads(net.ip('-j', '-d', 'link'))}
    if name in links:
        raise ValueError('group device already exists; deactivate first')
    for p in g['members']:
        if p not in links or 'master' in links[p] or 'UP' in links[p].get('flags', []):
            raise ValueError('members must exist, be administratively down and have no master')
        if links[p].get('linkinfo', {}).get('info_kind') in ('tun', 'veth', 'dummy', 'bond', 'bridge'):
            raise ValueError('activation requires physical member interfaces')
        if json.loads(net.ip('-j', 'address', 'show', 'dev', p))[0].get('addr_info'):
            raise ValueError('member still has addresses')
    return links


def activate(name, g):
    links = preflight(name, g)
    net.ip('link', 'add', name, 'type', 'bond', 'mode', '802.3ad',
           'miimon', '100', 'lacp_active', '1' if g['activity'] == 'active' else '0',
           'lacp_rate', '1' if g['rate'] == 'fast' else '0', 'min_links', str(g['min_links']),
           'ad_actor_sys_prio', str(g['system_priority']), 'xmit_hash_policy', g['hash'])
    attached = []
    try:
        net.ip('link', 'set', name, 'alias', 'ffn-lacp:' + name)
        for p in g['members']:
            attached.append(p)
            net.ip('link', 'set', p, 'mtu', str(g['network'].get('mtu', 1500)))
            net.ip('link', 'set', p, 'master', name)
            net.ip('link', 'set', p, 'up')
        net.configure_port(name, g['network'])
    except BaseException as original:
        errors = []
        for p in reversed(attached):
            try:
                net.ip('link', 'set', p, 'down')
                net.ip('link', 'set', p, 'nomaster')
                net.ip('link', 'set', p, 'address', links[p]['address'], 'mtu', str(links[p]['mtu']))
            except Exception as e:
                errors.append(str(e))
        try:
            net.ip('link', 'delete', name)
        except Exception as e:
            errors.append(str(e))
        raise RuntimeError('activation failed: %s; cleanup errors: %s' % (original, errors)) from original


def deactivate(name, g, live):
    if name not in live:
        return
    # Bond deletion releases members; retain their disabled network policy.
    for p in g['members']:
        net.ip('link', 'set', p, 'down')
    net.ip('link', 'delete', name)
    cfg = net.validate(json.loads(net.STATE.read_text()))
    for p in g['members']:
        net.configure_port(p, cfg['ports'][p])


def change(cfg, action, req):
    if type(req.get('revision')) is not int or req['revision'] != cfg['revision']:
        raise ValueError('revision conflict; fetch status first')
    live = runtime()
    if action == 'set':
        if set(req) != {'revision', 'groups'}:
            raise ValueError('set requires revision and groups')
        new = validate({'revision': cfg['revision'] + 1, 'groups': req['groups']})
        for name in live:
            if new['groups'].get(name) != cfg['groups'].get(name):
                raise ValueError('deactivate a group before editing or deleting it')
        save(new)
        return {'config': new}
    if set(req) != {'revision', 'group'} or req.get('group') not in cfg['groups']:
        raise ValueError('operation requires revision and a configured group')
    name = req['group']
    if action == 'activate':
        activate(name, cfg['groups'][name])
    elif action == 'deactivate':
        deactivate(name, cfg['groups'][name], live)
    else:
        raise ValueError('unknown action')
    return {'group': name, 'runtime': runtime(), 'config': cfg}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('status', 'set', 'activate', 'deactivate'))
    args = parser.parse_args()
    # Shares the network lock: bond membership and ordinary port edits cannot race.
    with open('/run/ffn-network.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cfg = load()
        if args.action == 'status':
            result = {'config': cfg, 'capabilities': capability(), 'runtime': runtime()}
        else:
            result = change(cfg, args.action, json.load(sys.stdin))
        print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
