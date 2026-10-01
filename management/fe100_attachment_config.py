"""Compile only committed interface/zone intent for the FE100 CP owner.

No addresses, credentials, table indices or hardware activation flags are sent.
The MP attests the complete configuration digest over its authenticated relay.
"""
import hashlib
import json
import re
from xml.etree import ElementTree as ET

MAX_INTERFACES=1024


def checksum(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def validate(value):
    if (not isinstance(value,dict) or set(value)!={'schema','config_digest','interfaces'} or
            type(value['schema']) is not int or value['schema']!=1 or
            not isinstance(value['config_digest'],str) or not re.fullmatch('[0-9a-f]{64}',value['config_digest']) or
            not isinstance(value['interfaces'],list) or len(value['interfaces'])>MAX_INTERFACES):
        raise ValueError('Invalid attachment configuration')
    seen=set()
    for row in value['interfaces']:
        if not isinstance(row,dict) or set(row)!={'name','parent','kind','ports','vlan','zone','vsys','enabled'}:
            raise ValueError('Incomplete interface intent')
        if row['kind'] not in ('physical','aggregate'):raise ValueError('Unsupported attachment kind')
        pattern=r'ethernet1/([1-9]|1[0-9]|2[0-4])' if row['kind']=='physical' else r'ae[1-9][0-9]?'
        if (not isinstance(row['parent'],str) or not re.fullmatch(pattern,row['parent']) or
                not isinstance(row['name'],str) or not re.fullmatch(re.escape(row['parent'])+r'(?:\.[1-9][0-9]{0,3})?',row['name']) or
                row['name'] in seen or type(row['enabled']) is not bool or
                type(row['vlan']) is not int or not 0<=row['vlan']<=4094 or
                (row['name']==row['parent'])!=(row['vlan']==0)):
            raise ValueError('Invalid interface identity or VLAN')
        ports=row['ports']
        if (not isinstance(ports,list) or not 1<=len(ports)<=8 or
                any(type(p) is not int or not 1<=p<=24 for p in ports) or ports!=sorted(set(ports)) or
                row['kind']=='physical' and ports!=[int(row['parent'].split('/')[1])]):
            raise ValueError('Invalid configured member mapping')
        for field in ('vsys','zone'):
            if row[field] is not None and (not isinstance(row[field],str) or not 1<=len(row[field])<=128):
                raise ValueError('Invalid zone ownership')
        if row['zone'] is not None and row['vsys'] is None:raise ValueError('Zone requires virtual system')
        seen.add(row['name'])
    return value


def compile_config(raw):
    if not isinstance(raw,bytes) or len(raw)>16*1024*1024:raise ValueError('Bounded configuration bytes required')
    # Decode first so UTF-16 or obfuscated DTDs cannot bypass this guard.
    text=raw.decode('utf-8')
    if re.search(r'<!\s*(DOCTYPE|ENTITY)\b',text,re.I):raise ValueError('Configuration declarations are forbidden')
    root=ET.fromstring(text);devices=root.findall('./devices/entry')
    if root.tag!='config' or len(devices)!=1:raise ValueError('One local device configuration required')
    device=devices[0];imports={};zones={};members={};result=[];names=set()
    for vsys in device.findall('./vsys/entry'):
        ident=vsys.get('name')
        if not ident:raise ValueError('Virtual system identity required')
        for member in vsys.findall('./import/network/interface/member'):
            imports.setdefault(member.text,set()).add(ident)
        for zone in vsys.findall('./zone/entry'):
            for member in zone.findall('./network/layer3/member'):
                zones.setdefault(member.text,[]).append((ident,zone.get('name')))
    ethernet=device.findall('./network/interface/ethernet/entry')
    for entry in ethernet:
        group=entry.findtext('aggregate-group')
        if group:
            match=re.fullmatch(r'ethernet1/([1-9]|1[0-9]|2[0-4])',entry.get('name',''))
            if not match or entry.find('layer3') is not None:raise ValueError('Invalid aggregate member')
            members.setdefault(group,[]).append((int(match[1]),entry.findtext('link-state','auto')!='down'))
    for kind,entries in [('physical',ethernet),('aggregate',device.findall('./network/interface/aggregate-ethernet/entry'))]:
        for entry in entries:
            parent=entry.get('name','')
            if parent in names:raise ValueError('Duplicate interface definition')
            names.add(parent)
            l3=entry.find('layer3')
            if l3 is None:continue
            enabled=entry.findtext('link-state','auto')!='down'
            if kind=='physical':
                match=re.fullmatch(r'ethernet1/([1-9]|1[0-9]|2[0-4])',parent)
                if not match:raise ValueError('Unknown front interface')
                ports=[int(match[1])]
            else:
                ports=sorted(p for p,_ in members.get(parent,[]))
                enabled=enabled and all(up for _,up in members.get(parent,[]))
            nodes=[] if entry.findtext('aggregate-only')=='yes' else [(parent,0,enabled)]
            for unit in l3.findall('./units/entry'):
                tag=unit.findtext('tag','')
                if not re.fullmatch('[1-9][0-9]{0,3}',tag):raise ValueError('Explicit VLAN tag required')
                nodes.append((unit.get('name',''),int(tag),enabled and unit.findtext('link-state','auto')!='down'))
            tags=set()
            for name,tag,up in nodes:
                if tag in tags:raise ValueError('Duplicate VLAN selector')
                tags.add(tag)
                owners=imports.get(name,imports.get(parent,set()));assignment=zones.get(name,[])
                if len(owners)>1 or len(assignment)>1:raise ValueError('Ambiguous interface/zone ownership')
                vsys=next(iter(owners),None);zone=None
                if assignment:
                    zoned_vsys,zone=assignment[0]
                    if vsys is not None and zoned_vsys!=vsys:raise ValueError('Interface zone crosses virtual systems')
                    vsys=zoned_vsys
                    if not zone:raise ValueError('Zone name required')
                result.append(dict(name=name,parent=parent,kind=kind,ports=ports,vlan=tag,vsys=vsys,zone=zone,enabled=up))
    return validate(dict(schema=1,config_digest=hashlib.sha256(raw).hexdigest(),interfaces=sorted(result,key=lambda r:r['name'])))
