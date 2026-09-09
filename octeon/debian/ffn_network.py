#!/usr/bin/env python3
"""Runtime per-port Linux L2/L3 configuration; TAP packet backend is separate.

All interfaces live in ffn-data, never in the management namespace. A revision
check and flock serialize MP changes. Only changed ports are reconfigured.
"""
import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess as S

NS = 'ffn-data'
STATE = Path('/etc/ffn/network.json')

def run(*args):
    result = S.run(args, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('%s: %s' % (' '.join(args), result.stderr.strip()))
    return result.stdout

def ip(*args):
    return run('ip', '-n', NS, *args)

def validate_port(name, settings):
    if not isinstance(settings, dict):
        raise ValueError('port settings must be an object')
    if not re.fullmatch(r'p(?:[1-9]|1[0-9]|2[0-4])', name):
        raise ValueError('port name must be p1..p24')
    if set(settings) - {'mode', 'vlans', 'pvid', 'addresses', 'mtu'}:
        raise ValueError('unknown per-port setting')
    mode = settings.get('mode')
    if mode not in ('disabled', 'l2', 'l3'):
        raise ValueError('mode must be disabled, l2 or l3')
    mtu = settings.get('mtu', 1500)
    if type(mtu) is not int or not 1280 <= mtu <= 9000:
        raise ValueError('mtu must be 1280..9000')
    if mode == 'l2':
        vlans = settings.get('vlans', [])
        if not vlans or any(type(v) is not int or not 1 <= v <= 4094 for v in vlans):
            raise ValueError('l2 requires allowed VLAN IDs 1..4094')
        if len(set(vlans)) != len(vlans):
            raise ValueError('duplicate VLAN')
        if 'pvid' in settings and (type(settings['pvid']) is not int or settings['pvid'] not in vlans):
            raise ValueError('native/access pvid must be an allowed VLAN')
        if 'addresses' in settings:
            raise ValueError('l2 ports cannot carry routed addresses')
    else:
        if 'vlans' in settings or 'pvid' in settings:
            raise ValueError('VLAN membership is only valid for l2 mode')
        if mode == 'disabled' and 'addresses' in settings:
            raise ValueError('disabled ports cannot carry addresses')
        for address in settings.get('addresses', []):
            addr = ipaddress.ip_interface(address)
            if addr.ip.is_multicast or addr.ip.is_unspecified or addr.ip.is_loopback:
                raise ValueError('invalid routed address')
    return settings

def validate(cfg):
    if set(cfg) != {'revision', 'ports'} or type(cfg['revision']) is not int or cfg['revision'] < 0:
        raise ValueError('configuration requires revision and ports')
    if not isinstance(cfg['ports'], dict):
        raise ValueError('ports must be an object')
    addresses = set()
    for name, settings in cfg['ports'].items():
        validate_port(name, settings)
        for address in settings.get('addresses', []):
            key = str(ipaddress.ip_interface(address).ip)
            if key in addresses:
                raise ValueError('duplicate local IP address')
            addresses.add(key)
    return cfg

def exists():
    return any(x['name'] == NS for x in json.loads(run('ip', '-j', 'netns', 'list') or '[]'))

def configure_port(name, settings, create=False):
    if create:
        run('ip', 'netns', 'exec', NS, 'ip', 'tuntap', 'add', 'dev', name, 'mode', 'tap')
    ip('link', 'set', name, 'down')
    current = json.loads(ip('-j', 'link', 'show', 'dev', name))[0]
    if 'master' in current:
        run('ip', 'netns', 'exec', NS, 'bridge', 'vlan', 'del', 'dev', name, 'vid', '1-4094')
        ip('link', 'set', name, 'nomaster')
    ip('address', 'flush', 'dev', name)
    ip('link', 'set', name, 'mtu', str(settings.get('mtu', 1500)))
    mode = settings['mode']
    if mode == 'l2':
        ip('link', 'set', name, 'master', 'br-data')
        for vlan in settings['vlans']:
            flags = ('pvid', 'untagged') if vlan == settings.get('pvid') else ()
            run('ip', 'netns', 'exec', NS, 'bridge', 'vlan', 'add', 'dev', name, 'vid', str(vlan), *flags)
    elif mode == 'l3':
        for address in settings.get('addresses', []):
            ip('address', 'add', address, 'dev', name)
    if mode != 'disabled':
        ip('link', 'set', name, 'up')

def start(cfg):
    if exists():
        raise RuntimeError('ffn-data already exists')
    run('ip', 'netns', 'add', NS)
    try:
        ip('link', 'set', 'lo', 'up')
        ip('link', 'add', 'br-data', 'type', 'bridge', 'vlan_filtering', '1', 'vlan_default_pvid', '0', 'stp_state', '1')
        ip('link', 'set', 'br-data', 'up')
        for setting in ('net.ipv4.ip_forward=1', 'net.ipv6.conf.all.forwarding=1',
                        'net.ipv4.conf.all.send_redirects=0', 'net.ipv4.conf.default.send_redirects=0'):
            run('ip', 'netns', 'exec', NS, 'python3', '-c',
                "import sys; from pathlib import Path; k,v=sys.argv[1].split('='); "
                "Path('/proc/sys/'+k.replace('.', '/')).write_text(v+'\\n')", setting)
        for name, settings in cfg['ports'].items():
            configure_port(name, settings, create=True)
    except BaseException:
        run('ip', 'netns', 'delete', NS)
        raise

def save(cfg):
    temp = STATE.with_suffix('.tmp')
    with temp.open('w') as f:
        json.dump(cfg, f, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    temp.replace(STATE)

def backend():
    path = Path('/run/ffn-fabric.json')
    if path.exists():
        state = json.loads(path.read_text())
        if Path('/proc', str(state['pid'])).exists():
            return state
    return {'transport': 'TAP; no physical backend attached', 'ports': []}

def patch(cfg, request):
    if (set(request) != {'revision', 'ports'} or type(request['revision']) is not int
            or request['revision'] != cfg['revision'] or not isinstance(request['ports'], dict)):
        raise ValueError('revision conflict or invalid request; fetch status first')
    new = json.loads(json.dumps(cfg))
    new['ports'].update(request['ports'])
    new['revision'] += 1
    validate(new)
    attachment = backend()
    for number in attachment['ports']:
        settings = new['ports'].get('p%d' % number, {})
        if settings.get('mtu', 1500) > attachment.get('max_mtu', 1500):
            raise ValueError('active fabric currently supports MTU up to 1500')
    if not exists():
        raise RuntimeError('network service is stopped')
    changed = [p for p in request['ports'] if cfg['ports'].get(p) != new['ports'][p]]
    applied = []
    try:
        for name in changed:
            applied.append(name)
            configure_port(name, new['ports'][name], create=name not in cfg['ports'])
        save(new)
    except BaseException as original:
        failures = []
        for name in reversed(applied):
            try:
                if name in cfg['ports']:
                    configure_port(name, cfg['ports'][name])
                else:
                    ip('link', 'delete', name)
            except Exception as error:
                failures.append(str(error))
        raise RuntimeError('update failed: %s; rollback errors: %s' % (original, failures)) from original
    return {'revision': new['revision'], 'changed_ports': changed, 'config': new}

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('check', 'apply', 'patch', 'stop', 'status'))
    p.add_argument('--config', type=Path, default=STATE)
    args = p.parse_args()
    global_state = args.config
    if global_state != STATE:
        if args.action != 'check':
            p.error('alternate config is permitted only for validation')
    with open('/run/ffn-network.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cfg = validate(json.loads(global_state.read_text()))
        if args.action == 'apply':
            start(cfg)
            result = {'started': True, 'config': cfg}
        elif args.action == 'patch':
            import sys
            result = patch(cfg, json.load(sys.stdin))
        elif args.action == 'stop':
            if exists():
                run('ip', 'netns', 'delete', NS)
            result = {'stopped': True}
        elif args.action == 'status':
            result = {'config': cfg, 'running': exists(), 'backend': backend()}
            if result['running']:
                result['interfaces'] = json.loads(ip('-j', 'address'))
                result['routes'] = json.loads(ip('-j', 'route'))
        else:
            result = {'validated': True, 'config': cfg}
        print(json.dumps(result, indent=2))

if __name__ == '__main__':
    main()
