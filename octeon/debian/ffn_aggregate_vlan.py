#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Owned 802.1Q attachments on the aggregate TAP, independent of LACP."""
import ipaddress
import json
import re


def validate(parent,network):
    from ffn_interface_management import validate as management
    units=network.get('units',[])
    if not isinstance(units,list) or len(units)>64:raise ValueError('Invalid aggregate VLAN unit list')
    names=set();tags=set()
    for unit in units:
        if not isinstance(unit,dict) or set(unit)!={'name','tag','addresses','mtu','management'}:raise ValueError('Invalid VLAN settings')
        name=unit['name'];tag=unit['tag']
        if not isinstance(name,str) or not re.fullmatch(re.escape(parent)+r'\.[1-9][0-9]{0,3}',name) or name in names:raise ValueError('Invalid or duplicate VLAN unit')
        if type(tag) is not int or not 1<=tag<=4094 or tag in tags:raise ValueError('Invalid or duplicate VLAN tag')
        if type(unit['mtu']) is not int or not 576<=unit['mtu']<=min(network['mtu'],1500):raise ValueError('Invalid VLAN MTU')
        addresses=unit['addresses']
        if not isinstance(addresses,list) or len(addresses)>32 or len(set(addresses))!=len(addresses):raise ValueError('Invalid VLAN addresses')
        for address in addresses:
            value=ipaddress.ip_interface(address)
            if str(value)!=address or value.version==6 and unit['mtu']<1280:raise ValueError('Invalid VLAN address or IPv6 MTU')
        management(unit['management']);names.add(name);tags.add(tag)
    return units


def owner(token,name):return 'ffn-aggregate:'+token+':'+name


def guard(parent):
    # Installed before creating any addressed child; covers future unit names.
    return ('table inet ffn_aggregate_'+parent+' {\n chain forward {\n'
            ' type filter hook forward priority -250; policy accept;\n'
            ' iifname { "'+parent+'", "'+parent+'.*" } counter drop;\n'
            ' oifname { "'+parent+'", "'+parent+'.*" } counter drop;\n }\n}\n')


def harden(namespace,name,run):
    for family,key,value in (('ipv4','arp_ignore',1),('ipv4','arp_announce',2),('ipv6','accept_ra',0),('ipv6','autoconf',0)):
        run('ip','netns','exec',namespace,'sysctl','-q','-w','net/'+family+'/conf/'+name+'/'+key+'='+str(value))


def reconcile(namespace,parent,token,network,ip,run):
    """Called with the network lock held and packet delivery withdrawn."""
    from ffn_interface_management import apply
    units=validate(parent,network);wanted={u['name']:u for u in units}
    links={l['ifname']:l for l in json.loads(ip('-d','-j','link'))}
    trunk=links.get(parent,{})
    if trunk.get('ifalias')!='ffn-aggregate:'+token:raise ValueError('Aggregate trunk ownership changed')
    owned={name:l for name,l in links.items() if l.get('ifalias')==owner(token,name) and re.fullmatch(re.escape(parent)+r'\.[1-9][0-9]{0,3}',name)}
    for name in wanted:
        if name in links and name not in owned:raise ValueError('Refusing to adopt foreign VLAN interface '+name)
    for name,link in owned.items():
        info=link.get('linkinfo',{});data=info.get('info_data',{});unit=wanted.get(name)
        matches=unit is not None and info.get('info_kind')=='vlan' and data.get('id')==unit['tag'] and data.get('protocol','802.1Q')=='802.1Q' and (link.get('link')==parent or link.get('link_index')==trunk.get('ifindex'))
        if not matches:
            ip('link','set',name,'down')
            apply(namespace,name,dict(addresses=[],management=dict(profile='',ping=False,tcp=[],udp=[],sources=[])),remove=True)
            ip('link','delete',name);links.pop(name)
    harden(namespace,parent,run)
    if units:ip('link','set',parent,'up')
    for unit in units:
        name=unit['name']
        if name not in links:
            ip('link','add','link',parent,'name',name,'type','vlan','id',str(unit['tag']))
            try:ip('link','set',name,'alias',owner(token,name))
            except Exception:
                ip('link','delete',name)
                raise
        ip('link','set',name,'down')
        harden(namespace,name,run)
        actual=[a for row in json.loads(ip('-j','address','show','dev',name)) for a in row.get('addr_info',[])]
        for address in actual:
            if address.get('scope')!='link':ip('address','del',str(address['local'])+'/'+str(address['prefixlen']),'dev',name)
        apply(namespace,name,dict(mode='l3',addresses=unit['addresses'],management=unit['management']))
        ip('link','set',name,'mtu',str(unit['mtu']))
        for address in unit['addresses']:ip('address','add',address,'dev',name)
        ip('link','set',name,'up')
    return [dict(name=u['name'],tag=u['tag'],addresses=u['addresses'],mtu=u['mtu'],management_profile=u['management']['profile']) for u in units]


def cleanup(namespace,parent,token,ip):
    from ffn_interface_management import apply
    for link in json.loads(ip('-d','-j','link')):
        name=link['ifname']
        if re.fullmatch(re.escape(parent)+r'\.[1-9][0-9]{0,3}',name) and link.get('ifalias')==owner(token,name):
            ip('link','set',name,'down')
            apply(namespace,name,dict(addresses=[],management=dict(profile='',ping=False,tcp=[],udp=[],sources=[])),remove=True)
            ip('link','delete',name)


def classify(parent,network,frame):
    """Return the configured attachment and normalized frame; never guess a VLAN."""
    if len(frame)<14:return None
    kind=frame[12:14];unit=None;plain=frame;overhead=14
    if kind==b'\x81\x00':
        if len(frame)<18:return None
        tag=int.from_bytes(frame[14:16],'big')&4095
        unit=next((u for u in network.get('units',[]) if u['tag']==tag),None)
        if unit is None or frame[16:18] in (b'\x81\x00',b'\x88\xa8'):return None
        plain=frame[:12]+frame[16:];overhead=18
    elif kind==b'\x88\xa8':return None
    elif network.get('enabled',True):unit=dict(name=parent,addresses=network['addresses'],mtu=network['mtu'])
    if unit is None or len(frame)>unit['mtu']+overhead:return None
    destination=None
    if plain[12:14]==b'\x08\x00' and len(plain)>=34:destination=plain[30:34]
    elif plain[12:14]==b'\x86\xdd' and len(plain)>=54:destination=plain[38:54]
    local=destination in {ipaddress.ip_interface(a).ip.packed for a in unit['addresses']}
    return unit['name'],plain,local


def carrying(network):return network.get('enabled',True) or bool(network.get('units'))
