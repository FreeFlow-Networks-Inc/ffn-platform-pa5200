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
        l3=entry.find('layer3');mode=entry.findtext('layer3/bond/mode','802.3ad')
        if l3 is None:errors.append('Hardware aggregate Layer 3 mode is required by this adapter')
        if mode not in ('802.3ad','lacp'):errors.append('Hardware aggregate mode '+mode+' is not implemented')
        if any(c.tag not in ('comment','layer3','lldp','link-state') for c in entry):errors.append('Unsupported aggregate options')
        for path,allowed in (
            ('layer3/bond',('mode','miimon')),
            ('layer3/lacp',('mode','transmission-rate','min-links','system-priority')),
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
        for address in addresses:
            try:ipaddress.ip_interface(address)
            except ValueError:errors.append('Invalid aggregate IP address')
        dhcp=entry.findtext('layer3/dhcp-client/enable','no')
        if dhcp not in ('yes','no'):errors.append('DHCP enable must be yes or no')
        if dhcp=='yes' and addresses:errors.append('Choose DHCP or static addresses, not both')
        if l3 is not None and any(c.tag not in ('bond','lacp','ip','dhcp-client','mtu','interface-management-profile','units') for c in l3):errors.append('Unsupported aggregate Layer 3 options')
        # VLAN units do not participate in LACP. Report their unsupported
        # attachment separately instead of disabling the parent control protocol.
        subinterfaces=[dict(name=e.get('name',''),tag=e.findtext('tag',''),applied=False,state='unsupported',
                           reason='Tagged aggregate subinterface attachment is not implemented in the dataplane')
                       for e in entry.findall('layer3/units/entry')]
        def number(path,default,low,high):
            try:
                value=int(entry.findtext(path,str(default)))
                if not low<=value<=high:raise ValueError()
                return value
            except ValueError:errors.append('Invalid '+path);return default
        activity=entry.findtext('layer3/lacp/mode','active');rate=entry.findtext('layer3/lacp/transmission-rate','fast')
        if activity not in ('active','passive'):errors.append('Invalid LACP activity')
        if rate not in ('fast','slow'):errors.append('Invalid LACP transmission rate')
        result.append(dict(ae_name=name,members=rows,bonding_mode=mode,
            enabled=entry.findtext('link-state','auto')!='down',miimon_ms=number('layer3/bond/miimon',100,1,10000),
            lacp=dict(activity=activity,rate=rate,min_links=number('layer3/lacp/min-links',1,1,max(1,len(rows))),
                      system_priority=number('layer3/lacp/system-priority',32768,1,65535)),
            network=dict(addresses=addresses,dhcp=dhcp=='yes',
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
    root=parse(raw);device=root.find("./devices/entry[@name='localhost.localdomain']")
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
