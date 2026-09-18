#!/usr/bin/env python3
"""Commit-only MP interface controller. Internal/backplane ports are excluded."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from defusedxml import ElementTree as ET
sys.path[:0]=['/opt/ffn-ngfw-v2','/opt/ffn-ngfw']
from ffn_mp_interfaces import decode, LABELS
import ffn_ifroles

RUNNING=Path('/var/lib/ffn-ngfw/config/running-config.xml')
NETWORK=Path('/etc/systemd/network')
BACKUPS=Path('/var/backups/ffn')


def run(*args):
    return subprocess.check_output(list(args),text=True,timeout=15).strip()


def inventory():
    addresses=json.loads(run('ip','-j','address','show'))
    routes=json.loads(run('ip','-j','-4','route','show','default'))
    result=[]
    for row in addresses:
        role=ffn_ifroles.classify(row['ifname'])
        if role['label'] not in LABELS or role['class'] not in ('management','ha','aux') or not role['pci']: continue
        try:
            output=run('resolvectl','dns',row['ifname'])
            dns=[str(ipaddress.ip_address(v)) for v in output.split(': ',1)[1].split()] if ': ' in output else []
        except (OSError,ValueError,subprocess.SubprocessError):dns=[]
        result.append(dict(dns=dns,dhcp=any(a.get('dynamic') for a in row['addr_info'] if a['family']=='inet'),name=role['label'],netdev=row['ifname'],pci=role['pci'],role=role['class'],
            link='LOWER_UP' in row['flags'],enabled='UP' in row['flags'],mtu=row['mtu'],mac=row.get('address'),
            addresses=[str(ipaddress.IPv4Interface('%s/%s'%(a['local'],a['prefixlen']))) for a in row['addr_info'] if a['family']=='inet'],
            gateway=next((r.get('gateway','') for r in routes if r.get('dev')==row['ifname']),'')))
    return sorted(result,key=lambda r:LABELS.index(r['name']))


def render(port,config):
    # Stable physical PCI match survives kernel interface renaming.
    text='[Match]\nPath=pci-%s\n\n[Link]\nMTUBytes=%d\nRequiredForOnline=no\nActivationPolicy=%s\n\n[Network]\nLinkLocalAddressing=ipv6\nIPv6AcceptRA=no\nDHCP=%s\n'%(port['pci'],config['mtu'],
        'always-down' if config['mode']=='disabled' else 'up', 'ipv4' if config['mode']=='dhcp' else 'no')
    if config['mode']=='static':
        text+='Address=%s\n'%config['address']
        if config['gateway']:text+='Gateway=%s\n'%config['gateway']
    for dns in config['dns']:text+='DNS=%s\n'%dns
    if config['mode']=='dhcp':text+='\n[DHCPv4]\nUseDNS=%s\nRouteMetric=100\n'%('no' if config['dns'] else 'yes')
    return text


def matches(port,config):
    return (port.get('mtu')==config['mtu'] and
        port.get('enabled') is (config['mode']!='disabled') and
        (config['mode']!='static' or config['address'] in port.get('addresses',[])) and
        (not config['gateway'] or config['gateway']==port.get('gateway')))


def execute(action,payload):
    raw=RUNNING.read_bytes(); digest=hashlib.sha256(raw).hexdigest()
    revision=int(digest[:12],16)
    ports=inventory()
    if action=='status' and not payload:return dict(revision=revision,ports=ports,owner='ffn-controld')
    if action!='apply' or payload!={'revision':revision,'digest':digest}:raise ValueError('Exact committed configuration revision required')
    if run('systemctl','is-active','systemd-networkd')!='active':raise ValueError('systemd-networkd is required')
    root=ET.fromstring(raw,forbid_dtd=True)
    entries=root.findall("./devices/entry[@name='localhost.localdomain']/deviceconfig/system/mp-interfaces/entry")
    detected={p['name']:p for p in ports}; changes=[]; desired={}
    for node in entries:
        name=node.get('name')
        if name in desired:raise ValueError('Duplicate MP interface entry')
        if name not in detected:raise ValueError('External MP port is not detected: '+str(name))
        config=decode(node); port=detected[name]
        desired[name]=config
        path=NETWORK/('05-ffn-mp-'+name.lower()+'.network')
        content=render(port,config); before=path.read_text() if path.exists() else None
        if before!=content or not matches(port,config):changes.append((port,path,content,before))
    # Omitted ports retain their current setup. No takeover on installation.
    if not changes:return dict(applied=[],revision=revision)
    if RUNNING.read_bytes()!=raw:raise ValueError('Committed configuration changed')
    backup=BACKUPS/('mp-interfaces-%d'%time.time_ns());backup.mkdir(parents=True,mode=0o700)
    written=[]
    try:
        for port,path,content,before in changes:
            (backup/(path.name+'.json')).write_text(json.dumps({'previous':before}))
            temp=path.with_suffix('.tmp');temp.write_text(content);os.chmod(temp,0o644);temp.replace(path);written.append((port,path,before))
        run('networkctl','reload')
        for port,path,before in written:run('networkctl','reconfigure',port['netdev'])
        deadline=time.monotonic()+5
        while True:
            observed={p['name']:p for p in inventory()}
            pending=[]
            for port,_,_ in written:
                cfg=desired[port['name']]; state=observed.get(port['name'],{})
                if not matches(state,cfg):pending.append(port['name'])
            if not pending:break
            if time.monotonic()>=deadline:raise ValueError('MP interface readback did not converge: '+', '.join(pending))
            time.sleep(.2)
    except Exception:
        for port,path,before in written:
            if before is None:path.unlink(missing_ok=True)
            else:path.write_text(before)
        run('networkctl','reload')
        for port,path,before in written:run('networkctl','reconfigure',port['netdev'])
        raise
    # networkd acceptance is not proof of DHCP lease or remote reachability.
    return dict(applied=[p['name'] for p,_,_ in written],revision=revision,state='networkd-reconfigured',ports=list(observed.values()))


if __name__=='__main__':
    import fcntl
    try:
        with open('/run/ffn-mp-interfaces.lock','w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            print(json.dumps(execute(sys.argv[1],json.load(sys.stdin))))
    except Exception as error:
        print(json.dumps({'error':str(error)}));raise SystemExit(2)
