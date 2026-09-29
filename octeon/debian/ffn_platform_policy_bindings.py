#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Discover active aggregate attachments from their DP owner's evidence.

Installed by the selected PA5200 platform. No network changes, guessed port
names, static VLAN configuration or persistent copies of ephemeral bindings.
"""
import json
from pathlib import Path
import re
import time
import uuid


def preview(xml,current,addresses):
    """Compile configured interfaces without requiring carrier or a live owner.

    This inventory is used only by nft --check. Apply and the forwarding lease
    continue to require discover() evidence from the real owner on this boot.
    """
    import copy
    import ipaddress
    from ffn_policy_config import parse
    from ffn_interface_addresses import resolved_config
    root=resolved_config(parse(xml))
    current=copy.deepcopy(current);addresses=copy.deepcopy(addresses);pending=[];seen=set();configured=set()
    index=0x70000000
    for device in root.findall('devices/entry'):
        parents=[(p,False) for p in device.findall('network/interface/ethernet/entry')]
        parents += [(p,True) for p in device.findall('network/interface/aggregate-ethernet/entry')]
        for parent,aggregate in parents:
            name=parent.get('name','')
            pattern=r'ae(?:[1-9]|1[0-2])' if aggregate else r'ethernet1/(?:[1-9]|1[0-9]|2[0-4])'
            if not re.fullmatch(pattern,name) or name in seen:
                raise ValueError('Invalid or duplicate candidate interface: '+name)
            seen.add(name)
            if aggregate:
                members=[e for e in device.findall('network/interface/ethernet/entry') if e.findtext('aggregate-group')==name]
                names=[e.get('name','') for e in members]
                if (not 2<=len(names)<=8 or len(set(names))!=len(names) or
                        any(not re.fullmatch(r'ethernet1/([1-9]|1[0-9]|2[0-4])',n) for n in names)):
                    raise ValueError(name+': aggregate requires 2..8 unique faceplate members')
                mode=parent.findtext('bond/mode',parent.findtext('layer3/bond/mode','802.3ad'))
                if mode not in ('802.3ad','lacp'):raise ValueError(name+': unsupported aggregate mode')
            layer=parent.find('layer3')
            if not aggregate and parent.find('aggregate-group') is not None and layer is not None:
                raise ValueError(name+': aggregate member cannot also have layer3 settings')
            rows=[]
            if layer is not None:
                if parent.findtext('aggregate-only')!='yes':rows.append((name,layer))
                tags=set()
                for unit in layer.findall('units/entry'):
                    child=unit.get('name','');tag=unit.findtext('tag','')
                    if (not re.fullmatch(re.escape(name)+r'\.[1-9][0-9]{0,3}',child) or
                            not tag.isdigit() or not 1<=int(tag)<=4094 or int(tag) in tags or child in seen):
                        raise ValueError(name+': invalid or duplicate aggregate VLAN unit')
                    tags.add(int(tag));seen.add(child);rows.append((child,unit))
            for logical,node in rows:
                configured.add(logical)
                values=[ipaddress.ip_interface(e.get('name','')) for e in node.findall('ip/entry')]
                if values and node.findtext('dhcp-client/enable')=='yes':raise ValueError(logical+': choose DHCP or static addresses')
                if logical not in current:
                    index+=1
                    while index in {r['index'] for r in current.values()}:index+=1
                    dev='ffnvp'+str(index-0x70000000)
                    while dev in addresses:dev+='x'
                    if len(dev)>15:raise ValueError('Candidate interface preview capacity exhausted')
                    current[logical]=dict(device=dev,index=index,alias='candidate-preview')
                    pending.append(logical)
                dev=current[logical]['device']
                addresses[dev]=dict(ifname=dev,addr_info=[dict(family='inet' if a.version==4 else 'inet6',
                    local=str(a.ip),prefixlen=a.network.prefixlen) for a in values])
    # The proposal, including removals/mode changes, is authoritative. Stale
    # runtime attachments must not validate references absent from this config.
    current={k:v for k,v in current.items() if k in configured}
    return current,addresses,pending


def trusted(path):
    st=path.stat()
    return st.st_uid==0 and not st.st_mode & 0o022


def security_guards(links):
    """Only current acknowledged owners may exchange their default-deny guard.

    The core replaces these tables atomically with Security/NAT and its closed
    lease gate. Aggregate restart still installs the original default-deny guard.
    """
    active=discover(links)
    parents=sorted({name.split('.')[0] for name in active if re.fullmatch(r'ae[1-9][0-9]*(?:\.[1-9][0-9]{0,3})?',name)})
    result={}
    for parent in parents:
        table='ffn_aggregate_'+parent
        result[table]=('table inet '+table+' {\n chain forward {\n'
            ' type filter hook forward priority -250; policy accept;\n'
            ' iifname { "'+parent+'", "'+parent+'.*" } meta mark & 0x80000000 == 0 counter drop;\n'
            ' oifname { "'+parent+'", "'+parent+'.*" } meta mark & 0x80000000 == 0 counter drop;\n }\n}\n')
    return result


def wan_binding(links,run,proc,now,boot):
    """Discover the existing WAN owner's port; never persist customer bindings."""
    path=run/'ffn-fabric.json'
    try:
        if not trusted(path):return {}
        row=json.loads(path.read_text())
        if (row.get('owner')!='wan1' or row.get('boot_id')!=boot or row.get('ports')!=[1]
                or not 0<=now-row['updated_monotonic']<=5):return {}
        pid=row['pid']
        if type(pid) is not int or pid<=1:return {}
        process=(proc/str(pid)/'stat').read_text().rsplit(') ',1)[1].split()
        if process[19]!=str(row['process_start']) or process[0]=='Z':return {}
        link=links.get('p1',{})
        if (type(link.get('ifindex')) is not int or row.get('interfaces',{}).get('p1')!=link['ifindex']
                or link.get('master') or 'UP' not in link.get('flags',[])
                or link.get('linkinfo',{}).get('info_kind')!='tun'
                or link.get('linkinfo',{}).get('info_data',{}).get('type')!='tap'):return {}
        return {'ethernet1/1':'p1'}
    except (OSError,ValueError,TypeError,KeyError,IndexError):return {}


def physical_bindings(links,run=Path('/run'),proc=Path('/proc'),now=None):
    """Current physical TAP process evidence, independent of physical carrier."""
    now=time.monotonic() if now is None else now
    boot=(proc/'sys/kernel/random/boot_id').read_text().strip();result={}
    for path in run.glob('ffn-physical-*-status.json'):
        try:
            if not trusted(path):continue
            row=json.loads(path.read_text());ports=row['ports']
            if not isinstance(ports,list) or len(ports)!=1 or type(ports[0]) is not int or not 2<=ports[0]<=24:continue
            port=ports[0];name='p'+str(port)
            if path.name!='ffn-physical-'+str(port)+'-status.json' or row['owner']!='physical-'+str(port):continue
            if row['boot_id']!=boot or not 0<=now-row['updated_monotonic']<=5:continue
            if type(row['pid']) is not int or row['pid']<=1:continue
            process=(proc/str(row['pid'])/'stat').read_text().rsplit(') ',1)[1].split()
            if process[0]=='Z' or process[19]!=str(row['process_start']):continue
            link=links.get(name,{})
            if (type(link.get('ifindex')) is not int or row.get('interfaces',{}).get(name)!=link['ifindex'] or link.get('master') or
                link.get('linkinfo',{}).get('info_kind')!='tun' or link.get('linkinfo',{}).get('info_data',{}).get('type')!='tap'):continue
            result['ethernet1/'+str(port)]=name
        except (OSError,ValueError,TypeError,KeyError,IndexError):continue
    return result


def discover(links,run=Path('/run'),proc=Path('/proc'),now=None):
    now=time.monotonic() if now is None else now
    boot=(proc/'sys/kernel/random/boot_id').read_text().strip()
    result=wan_binding(links,run,proc,now,boot)
    result.update(physical_bindings(links,run,proc,now))
    for path in run.glob('ffn-aggregate-*-status.json'):
        try:
            if not trusted(path):continue
            row=json.loads(path.read_text());name=row['group'];token=row['token']
            if not isinstance(name,str) or not re.fullmatch(r'ae[1-9][0-9]*',name):continue
            if path.name!='ffn-aggregate-'+name+'-status.json' or str(uuid.UUID(token))!=token:continue
            if row['boot_id']!=boot or not 0<=now-row['updated_monotonic']<=5:continue
            if (row.get('control_only') or row.get('fault') or not row.get('gates_verified') or
                not row.get('configuration_ready',row.get('network_ready')) or row.get('network_update_pending')):continue
            pid=row['pid']
            if type(pid) is not int or pid<=1:continue
            process=(proc/str(pid)/'stat').read_text().rsplit(') ',1)[1].split()
            if process[19]!=str(row['process_start']) or process[0]=='Z':continue
            if not re.fullmatch(r'[0-9a-f]{64}',row.get('configuration_revision','')):continue
            parent=links.get(name,{})
            if parent.get('ifalias')!='ffn-aggregate:'+token or parent.get('master'):continue
            if row.get('attachment_ready') and row.get('network',{}).get('enabled',True):result[name]=name
            for child in row.get('subinterfaces',[]):
                unit=child['name'];link=links.get(unit,{})
                if not isinstance(unit,str) or not re.fullmatch(re.escape(name)+r'\.[1-9][0-9]{0,3}',unit):continue
                info=link.get('linkinfo',{});data=info.get('info_data',{})
                if (child.get('applied') is True and link.get('ifalias')=='ffn-aggregate:'+token+':'+unit and
                    not link.get('master') and info.get('info_kind')=='vlan' and data.get('protocol','802.1Q')=='802.1Q' and
                    type(child.get('tag')) is int and data.get('id')==child['tag'] and
                    (link.get('link')==name or link.get('link_index')==parent.get('ifindex'))):result[unit]=unit
        except (OSError,ValueError,TypeError,KeyError,IndexError):
            # Missing/stale evidence withdraws only this owner. The caller fails
            # validation if its plan requires one of the withdrawn interfaces.
            continue
    return result
