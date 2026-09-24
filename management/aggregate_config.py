# SPDX-License-Identifier: GPL-2.0-or-later
"""PA-5200 aggregate intent and readiness shared by MP status and configd.

This is the hardware contract. It does not translate BCM faceplate ports into
management-CPU Linux NICs or treat a saved Linux LACP profile as an applied AE.
"""
import hashlib
from collections import Counter
import ipaddress
import re
from xml.etree import ElementTree as ET

PORTS=(28,13,14,15,16,1,18,19,6,21,22,23,7,11,36,27,10,29,30,31,32,33,34,35)


def vlan_units(device,entry):
    from ffn_interface_management import profile
    result=[];nodes=[(mode,e) for mode in ('layer3','layer2') for e in entry.findall(mode+'/units/entry')]
    names=Counter(e.get('name') for _,e in nodes);tags=Counter(e.findtext('tag') for _,e in nodes)
    for mode,node in nodes:
        row=dict(name=node.get('name',''),tag=node.findtext('tag',''),mode=mode,applied=False,state='pending',reason='Awaiting dataplane acknowledgement')
        try:
            if mode!='layer3':raise ValueError('Layer 2 aggregate VLAN attachment is not implemented')
            if len(nodes)>64:raise ValueError('At most 64 aggregate VLAN units are supported')
            if not re.fullmatch(re.escape(entry.get('name',''))+r'\.[1-9][0-9]{0,3}',row['name']):raise ValueError('Invalid aggregate unit name')
            if names[row['name']]!=1 or tags[row['tag']]!=1:raise ValueError('Duplicate unit name or VLAN tag')
            if set(node.attrib)!={'name'}:raise ValueError('Unsupported VLAN unit attributes')
            if any(n.tag not in ('tag','ip','mtu','interface-management-profile','comment') for n in node):raise ValueError('Unsupported aggregate VLAN options')
            if len({n.tag for n in node})!=len(node):raise ValueError('Duplicate aggregate VLAN settings')
            for setting in node:
                if setting.attrib or setting.tag!='ip' and len(setting):raise ValueError('Unsupported VLAN setting structure')
                if setting.tag=='ip' and any(e.tag!='entry' or set(e.attrib)!={'name'} or len(e) for e in setting):raise ValueError('Unsupported VLAN IP settings')
            if not re.fullmatch(r'[1-9][0-9]{0,3}',row['tag']):raise ValueError('Invalid VLAN tag')
            row['tag']=int(row['tag'])
            if not 1<=row['tag']<=4094:raise ValueError('VLAN tag must be 1..4094')
            row['addresses']=[str(ipaddress.ip_interface(e.get('name',''))) for e in node.findall('ip/entry')]
            if len(row['addresses'])>32 or len(set(row['addresses']))!=len(row['addresses']):raise ValueError('Invalid or duplicate VLAN addresses')
            parent_mtu=int(entry.findtext('layer3/mtu','1500'));row['mtu']=int(node.findtext('mtu',str(parent_mtu)))
            if not 576<=row['mtu']<=min(1500,parent_mtu) or any(':' in a for a in row['addresses']) and row['mtu']<1280:raise ValueError('VLAN MTU exceeds parent or protocol limits')
            row['management']=profile(device,node.findtext('interface-management-profile',''))
            row['supported']=True
        except (ValueError,TypeError) as error:row.update(supported=False,state='unsupported',reason=str(error))
        result.append(row)
    return result


def parse(raw):
    if isinstance(raw,bytes):raw=raw.decode('utf-8')
    if len(raw)>16*1024*1024 or re.search(r'<!\s*(DOCTYPE|ENTITY)\b',raw,re.I):raise ValueError('Unsafe aggregate XML')
    return ET.fromstring(raw)


def compile_device(device):
    result=[];members={};orphans=[]
    for entry in device.findall('./network/interface/ethernet/entry'):
        group=entry.findtext('aggregate-group')
        if group is not None:members.setdefault(group,[]).append(entry)
    names=set()
    for entry in device.findall('./network/interface/aggregate-ethernet/entry'):
        name=entry.get('name','');errors=[];rows=[]
        if not re.fullmatch(r'ae(?:[1-9]|1[0-2])',name):errors.append('Hardware aggregate name must be ae1..ae12')
        if name in names:errors.append('Duplicate aggregate definition')
        names.add(name)
        for member in members.get(name,[]):
            pan=member.get('name','');match=re.fullmatch(r'ethernet1/([1-9]|1[0-9]|2[0-4])',pan)
            if not match:errors.append('Unmapped member '+pan);continue
            port=int(match[1]);rows.append(dict(name=pan,port=port,bcm_port=PORTS[port-1],
                enabled=member.findtext('link-state','auto')!='down',speed=member.findtext('link-speed','auto')))
            if member.findtext('link-state','auto') not in ('auto','up','down'):errors.append(pan+': invalid link state')
            if member.findtext('link-duplex','auto') not in ('auto','full'):errors.append(pan+': full duplex required')
            if any(c.tag not in ('comment','aggregate-group','link-state','link-speed','link-duplex','lldp') for c in member):
                errors.append(pan+': aggregate members cannot carry independent interface settings')
            lldp=member.find('lldp')
            if lldp is not None and (lldp.attrib or any(n.tag!='enable' or n.text!='no' or n.attrib or len(n) for n in lldp)):
                errors.append(pan+': aggregate member LLDP must be disabled')
        if not 2<=len(rows)<=8:errors.append('Aggregate requires 2..8 members')
        if len({r['port'] for r in rows})!=len(rows):errors.append('Duplicate aggregate member')
        l3=entry.find('layer3');bond='bond' if entry.find('bond') is not None else 'layer3/bond'
        lacp='lacp' if entry.find('lacp') is not None else 'layer3/lacp'
        mode=entry.findtext(bond+'/mode','802.3ad')
        network_enabled=l3 is not None and entry.findtext('aggregate-only')!='yes'
        if entry.find('layer2') is not None and entry.findtext('aggregate-only')!='yes':errors.append('Aggregate Layer 2 attachment is not implemented in the dataplane')
        if entry.findtext('aggregate-only','no') not in ('yes','no'):errors.append('Invalid aggregate-only flag')
        if mode not in ('802.3ad','lacp'):errors.append('Hardware aggregate mode '+mode+' is not implemented')
        if any(c.tag not in ('comment','layer3','layer2','lldp','link-state','bond','lacp','aggregate-only') for c in entry):errors.append('Unsupported aggregate options')
        for path,allowed in (
            ('layer3/bond',('mode','miimon')),
            ('bond',('mode','miimon')),
            ('layer3/lacp',('mode','transmission-rate','min-links','system-priority')),
            ('lacp',('mode','transmission-rate','min-links','system-priority')),
            ('layer3/dhcp-client',('enable','create-default-route','default-route-metric')),
            ('lldp',('enable',)),
        ):
            parent=entry.find(path)
            if parent is not None and any(n.tag not in allowed for n in parent):errors.append('Unsupported '+path+' options')
        for path,default,allowed in (
            ('layer3/dhcp-client/create-default-route','no',('yes','no')),
            ('lldp/enable','no',('yes','no')),('link-state','auto',('up','down','auto')),
        ):
            if entry.findtext(path,default) not in allowed:errors.append('Invalid '+path)
        addresses=[n.get('name','') for n in entry.findall('./layer3/ip/entry')]
        if not network_enabled and (addresses or entry.findtext('layer3/dhcp-client/enable')=='yes' or entry.findtext('layer3/interface-management-profile')):
            errors.append('Link-only aggregates cannot have parent addressing or management profiles')
        for address in addresses:
            try:ipaddress.ip_interface(address)
            except ValueError:errors.append('Invalid aggregate IP address')
        dhcp=entry.findtext('layer3/dhcp-client/enable','no')
        if dhcp not in ('yes','no'):errors.append('DHCP enable must be yes or no')
        if dhcp=='yes' and addresses:errors.append('Choose DHCP or static addresses, not both')
        if l3 is not None and any(c.tag not in ('bond','lacp','ip','dhcp-client','mtu','interface-management-profile','units') for c in l3):errors.append('Unsupported aggregate Layer 3 options')
        # Unit failures are reported separately and never disable parent LACP.
        subinterfaces=vlan_units(device,entry)
        def number(path,default,low,high):
            try:
                value=int(entry.findtext(path,str(default)))
                if not low<=value<=high:raise ValueError()
                return value
            except ValueError:errors.append('Invalid '+path);return default
        activity=entry.findtext(lacp+'/mode','active');rate=entry.findtext(lacp+'/transmission-rate','fast')
        if activity not in ('active','passive'):errors.append('Invalid LACP activity')
        if rate not in ('fast','slow'):errors.append('Invalid LACP transmission rate')
        result.append(dict(ae_name=name,members=rows,bonding_mode=mode,
            enabled=entry.findtext('link-state','auto')!='down',miimon_ms=number(bond+'/miimon',100,1,10000),
            lacp=dict(activity=activity,rate=rate,min_links=number(lacp+'/min-links',1,1,max(1,len(rows))),
                      system_priority=number(lacp+'/system-priority',32768,1,65535)),
            network=dict(addresses=addresses,dhcp=dhcp=='yes',
                         enabled=network_enabled,
                         dhcp_default_route=entry.findtext('layer3/dhcp-client/create-default-route','no')=='yes',
                         dhcp_route_metric=number('layer3/dhcp-client/default-route-metric',10,1,65535),
                         mtu=number('layer3/mtu',1500,576,9216),
                         management_profile=entry.findtext('layer3/interface-management-profile','')),
            lldp=entry.findtext('lldp/enable','no')=='yes',subinterfaces=subinterfaces,errors=errors))
    for name,entries in members.items():
        if name not in names:
            orphans.extend(dict(name=e.get('name',''),group=name,error='Aggregate '+name+' does not exist') for e in entries)
    return result,orphans


def plan(raw):
    from ffn_interface_addresses import resolved_config
    root=resolved_config(parse(raw));device=root.find("./devices/entry[@name='localhost.localdomain']")
    groups,orphans=compile_device(device) if device is not None else ([],[])
    return dict(revision=hashlib.sha256(raw if isinstance(raw,bytes) else raw.encode()).hexdigest(),
                aggregates=groups,orphan_members=orphans)


def readiness(group,faceplate,network,observations):
    ports={p['port']:p for p in faceplate.get('ports',[])}
    peers={p['port']:p for p in observations.get('ports',[]) if not p.get('expired',True)} if observations.get('available') else {}
    blocked=[dict(code='configuration',message=e) for e in group['errors']]
    members=[]
    for item in group['members']:
        live=ports.get(item['port'],{});peer=peers.get(item['port'])
        speed=live.get('speed_mbps') if live.get('link') is True else None
        members.append(dict(item,link=live.get('link'),admin_enabled=live.get('enabled'),
            speed_mbps=speed,reported_speed_mbps=live.get('speed_mbps'),available=live.get('available',False),
            partner_observation=peer,attached=item['port'] in network.get('backend',{}).get('ports',[])))
        if not live.get('available'):blocked.append(dict(code='member-unavailable',message=item['name']+': hardware observation unavailable'))
        elif group['enabled'] and item['enabled']:
            if live.get('enabled') is False:
                blocked.append(dict(code='member-admin-down',message=item['name']+': hardware is administratively disabled despite enabled configuration'))
            elif live.get('link') is False:
                blocked.append(dict(code='member-link-down',message=item['name']+': physical carrier is down'))
            elif live.get('link') is not True:
                blocked.append(dict(code='member-link-unknown',message=item['name']+': physical carrier is unknown'))
    speeds={p['speed_mbps'] for p in members if p['link'] and p['speed_mbps']}
    if len(speeds)>1:blocked.append(dict(code='member-speed',message='Members have different negotiated speeds'))
    actors=[p['partner_observation']['actor'] for p in members if p['partner_observation']]
    systems={(p.get('system_priority'),p['system'],p['key']) for p in actors}
    if len(systems)>1:blocked.append(dict(code='partner-mismatch',message='Members advertise different LACP partner system priorities, system MACs or keys'))
    identities=Counter((p['system'],p.get('port')) for p in actors if p.get('port'))
    duplicate=any(count>1 for count in identities.values())
    if duplicate:blocked.append(dict(code='partner-port-duplicate',message='Multiple members advertise the same LACP partner port; separate peer port identities are required'))
    complete=len(actors)==len(members) and all(p.get('port') and p.get('system_priority') is not None for p in actors)
    partner_consistency=dict(state='mismatch' if len(systems)>1 or duplicate else 'consistent' if complete else 'incomplete',
        observed_members=len(actors),expected_members=len(members),
        missing_members=[p['name'] for p in members if not p['partner_observation']],
        # Consistency is only a property of advertisements, never proof of vPC
        # peer-link health, local selection or successful packet forwarding.
        negotiated=False,forwarding_verified=False)
    # Only a real apply owner can replace these blockers with verified hardware
    # acknowledgments. Carrier or a peer PDU alone never enables forwarding.
    blocked.extend([
        dict(code='bcm-membership',message='BCM aggregate membership and hashing are not commissioned'),
        dict(code='lacp-negotiation',message='LACP transmit/receive and member selection are not commissioned'),
        dict(code='dataplane-attachment',message='Aggregate packet attachment to the OCTEON dataplane is not commissioned')])
    if group['network']['dhcp']:blocked.append(dict(code='dhcp-client',message='Aggregate DHCP lease application is not commissioned'))
    if group['lldp']:blocked.append(dict(code='lldp',message='Aggregate LLDP transmission is not commissioned'))
    return dict(group,members=members,owner='ffn-controld',backend='pa5200-bcm',
        state='blocked',applied=False,activation_supported=False,blockers=blocked,partner_consistency=partner_consistency,
        # Compatibility fields consumed by the common interface view.
        bond=None,kernel_exists=False,kernel_slaves=[],members_pan=[p['name'] for p in members],
        members_linux=[],operstate='BLOCKED',ip_addresses=[],observation_only=True)
