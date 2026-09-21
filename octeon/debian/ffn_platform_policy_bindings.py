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


def trusted(path):
    st=path.stat()
    return st.st_uid==0 and not st.st_mode & 0o022


def security_guards(links):
    """Only current acknowledged owners may exchange their default-deny guard.

    The core replaces these tables atomically with Security/NAT and its closed
    lease gate. Aggregate restart still installs the original default-deny guard.
    """
    active=discover(links)
    parents=sorted({name.split('.')[0] for name in active})
    result={}
    for parent in parents:
        table='ffn_aggregate_'+parent
        result[table]=('table inet '+table+' {\n chain forward {\n'
            ' type filter hook forward priority -250; policy accept;\n'
            ' iifname { "'+parent+'", "'+parent+'.*" } meta mark & 0x80000000 == 0 counter drop;\n'
            ' oifname { "'+parent+'", "'+parent+'.*" } meta mark & 0x80000000 == 0 counter drop;\n }\n}\n')
    return result


def discover(links,run=Path('/run'),proc=Path('/proc'),now=None):
    now=time.monotonic() if now is None else now
    boot=(proc/'sys/kernel/random/boot_id').read_text().strip()
    result={}
    for path in run.glob('ffn-aggregate-*-status.json'):
        try:
            if not trusted(path):continue
            row=json.loads(path.read_text());name=row['group'];token=row['token']
            if not isinstance(name,str) or not re.fullmatch(r'ae[1-9][0-9]*',name):continue
            if path.name!='ffn-aggregate-'+name+'-status.json' or str(uuid.UUID(token))!=token:continue
            if row['boot_id']!=boot or not 0<=now-row['updated_monotonic']<=5:continue
            if (row.get('control_only') or row.get('fault') or not row.get('gates_verified') or
                not row.get('distributing') or not row.get('network_ready') or row.get('network_update_pending')):continue
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
