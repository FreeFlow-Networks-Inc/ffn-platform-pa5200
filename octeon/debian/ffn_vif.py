#!/usr/bin/env python3
"""FFN VIF assignments and Ethernet adaptation; no implicit hardware access.

Reference: VM if_vif/fabric_vif modules and pan_vifconfig.sh. Linux TAP replaces
private RX callbacks; explicit port/VLAN selectors replace implicit ownership.
"""
import copy
import re
import json
import ipaddress
from pathlib import Path

TAGS=(b'\x81\x00',b'\x88\xa8')


def validate(config):
    from ffn_network import validate_port
    if (not isinstance(config,dict) or set(config)!={'revision','vifs'} or
        type(config['revision']) is not int or not 0<=config['revision']<2**63 or
        not isinstance(config['vifs'],dict) or len(config['vifs'])>256):
        raise ValueError('revision and at most 256 VIF assignments required')
    selectors=set();addresses=set()
    for name,b in config['vifs'].items():
        if not isinstance(name,str) or not re.fullmatch(r'fv[1-9][0-9]{0,3}',name) or int(name[2:])>4094:
            raise ValueError('VIF name must be fv1..fv4094')
        if (not isinstance(b,dict) or set(b)!={'port','vlan','enabled','network'} or
            type(b['port']) is not int or not 1<=b['port']<=24 or type(b['enabled']) is not bool or
            (b['vlan'] is not None and (type(b['vlan']) is not int or not 1<=b['vlan']<=4094))):
            raise ValueError('invalid VIF port/VLAN/enabled assignment')
        selector=(b['port'],b['vlan'])
        if selector in selectors:raise ValueError('duplicate ingress port/VLAN selector')
        selectors.add(selector)
        n=validate_port('p1',b['network'])
        if n.get('mtu',1500)>1500:raise ValueError('VIF transport supports MTU up to 1500')
        if n['mode']=='l2' and n.get('vlans')!=[n.get('pvid')]:
            raise ValueError('L2 VIF requires exactly one bridge VLAN, also its pvid')
        for address in n.get('addresses',[]):
            import ipaddress
            key=(n.get('vrf'),str(ipaddress.ip_interface(address).ip))
            if key in addresses:raise ValueError('duplicate VIF address in a virtual router')
            addresses.add(key)
    return copy.deepcopy(config)


def protect_network(network_config,path=Path('/etc/ffn/vifs.json')):
    """Prevent another network editor from invalidating retained assignments."""
    if not path.exists():return
    config=validate(json.loads(path.read_text()))
    occupied={(p.get('vrf'),str(ipaddress.ip_interface(a).ip))
              for p in network_config['ports'].values() for a in p.get('addresses',[])}
    for b in config['vifs'].values():
        n=b['network'];vrf=n.get('vrf')
        if vrf and vrf not in network_config.get('vrfs',{}):
            raise ValueError('remove dependent VIF assignments before deleting a virtual router')
        if any((vrf,str(ipaddress.ip_interface(a).ip)) in occupied for a in n.get('addresses',[])):
            raise ValueError('network address already belongs to a VIF')


class Assignments:
    def __init__(self,config,ports):
        self.config=validate(config)
        if any(b['port'] not in ports for b in self.config['vifs'].values()):
            raise ValueError('assignment requires a commissioned transport port')
        self.bindings=self.config['vifs']
        self.rx={(b['port'],b['vlan']):name for name,b in self.bindings.items()}

    def ingress(self,port,frame):
        if not 14<=len(frame)<=1518:return None
        vlan=None;payload=frame
        if frame[12:14] in TAGS:
            if frame[12:14]!=TAGS[0] or len(frame)<18:return None
            vlan=int.from_bytes(frame[14:16],'big')&4095
            if vlan in (0,4095) or frame[16:18] in TAGS:return None
            payload=frame[:12]+frame[16:]
        name=self.rx.get((port,vlan))
        if name is None:return None
        b=self.bindings[name]
        if not b['enabled'] or b['network']['mode']=='disabled' or len(payload)>14+b['network'].get('mtu',1500):return None
        return name,payload

    def egress(self,name,frame):
        b=self.bindings.get(name)
        if (not b or not b['enabled'] or b['network']['mode']=='disabled' or
            not 14<=len(frame)<=14+b['network'].get('mtu',1500) or frame[12:14] in TAGS):return None
        if b['vlan'] is not None:
            frame=frame[:12]+TAGS[0]+b['vlan'].to_bytes(2,'big')+frame[12:]
        return b['port'],frame
