"""Apply committed PA-5200 ethernet configuration through the MP owner daemon."""
import json
import re
import subprocess
import uuid
import ipaddress
import sqlite3
from xml.etree import ElementTree as ET


def rpc(resource, action='status', payload=None):
    from ffn_controld_client import ControldClient
    request={'v':1,'id':str(uuid.uuid4()),'resource':resource,'action':action,'payload':payload or {}}
    response=ControldClient(timeout=130).plane_request(request)
    if not response.get('ok'):
        raise ValueError('MP request %s: %s (%s)'%(request['id'],response.get('state'),response.get('error')))
    return response['result']


class PlatformApplier:
    def __init__(self, config): self.config=config

    def claims(self, xpath):
        return ('.network.interface.ethernet.' in xpath or '.network.interface.aggregate-ethernet.' in xpath
                or '.network.profiles.interface-management-profile.' in xpath
                or '.network.virtual-router.' in xpath)

    def reconcile(self, status):
        root=ET.parse(self.config).getroot()
        device=root.find("./devices/entry[@name='localhost.localdomain']")
        if device is None: return
        faceplate=rpc('faceplate')
        network=rpc('network')
        from aggregate_config import compile_device,readiness
        groups,orphans=compile_device(device)
        aggregate_errors={g['ae_name']:'; '.join(b['message'] for b in readiness(g,faceplate,network,{})['blockers']) for g in groups}
        patches={}
        requested=[]
        for entry in device.findall('./network/interface/ethernet/entry'):
            name=entry.get('name','');match=re.fullmatch(r'ethernet1/([1-9]|1[0-9]|2[0-4])',name)
            if not match:
                status.fail(name,'pa5200','Unmapped faceplate interface');continue
            port=int(match[1]);key='p%d'%port
            if entry.find('aggregate-group') is not None:
                group=entry.findtext('aggregate-group','')
                status.fail(name,'pa5200',group+': '+aggregate_errors.get(group,'Aggregate definition does not exist'));continue
            state=entry.findtext('link-state','auto')
            if state not in ('up','down','auto'):
                status.fail(name,'pa5200','Unsupported link-state');continue
            enabled=state!='down'
            observed=next((p for p in faceplate['ports'] if p['port']==port),None)
            if not observed or not observed['available']:
                status.fail(name,'pa5200','Faceplate port unavailable');continue
            speed=entry.findtext('link-speed','auto')
            if entry.findtext('link-duplex','auto') not in ('auto','full'):
                status.fail(name,'pa5200','Half duplex is not supported');continue
            if entry.find('link-speed') is not None or observed.get('speed_configuration'):
                if not observed.get('speed_configuration') or speed not in ['auto']+[str(v) for v in observed.get('supported_speeds',[])]:
                    status.fail(name+'/link-speed','pa5200','Requested speed is unavailable');continue
                if observed.get('configured_speed')!=speed:
                    answer=rpc('faceplate','apply',{'revision':faceplate['revision'],'port':port,'speed':speed})
                    faceplate=answer['data']
                status.ok(name+'/link-speed',None,speed,'pa5200','SDK link setting read back through MP daemon')
            if observed['enabled']!=enabled:
                answer=rpc('faceplate','apply',{'revision':faceplate['revision'],'port':port,'enabled':enabled})
                faceplate=answer['data']
            status.ok(name+'/link-state',None,state,'pa5200','Physical administrative state verified through MP daemon')
            if key not in network['config']['ports']:
                status.fail(name,'pa5200','No commissioned dataplane attachment for this port');continue
            unsupported=[child.tag for child in entry if child.tag not in ('comment','link-state','link-speed','link-duplex','layer3')]
            l3=entry.find('layer3')
            if unsupported or l3 is None or any(c.tag not in ('ip','mtu','interface-management-profile') for c in l3):
                status.fail(name,'pa5200','Interface mode or option is not implemented by this config adapter');continue
            desired={'mode':'l3','addresses':[e.get('name') for e in l3.findall('./ip/entry')]} if enabled else {'mode':'disabled'}
            if enabled:
                from ffn_interface_management import profile
                try:
                    desired['management']=profile(device,l3.findtext('interface-management-profile',''))
                except ValueError as error:
                    status.fail(name,'pa5200',str(error));continue
                # An empty editor placeholder must not activate an unattached
                # optical port or prevent a different interface from applying.
                if not desired['addresses'] and not desired['management']['profile']:
                    desired={'mode':'disabled'}
            if l3.findtext('mtu'): desired['mtu']=int(l3.findtext('mtu'))
            patches[key]=desired;requested.append((name,key,enabled))
        for entry in device.findall('./network/interface/aggregate-ethernet/entry'):
            name=entry.get('name','aggregate')
            status.fail(name,'pa5200',aggregate_errors.get(name,'Invalid aggregate definition'))
        if (patches.get('p1',{}).get('addresses') and 1 not in network.get('backend',{}).get('ports',[])):
            try:
                wan=rpc('wan-path')
                rpc('wan-path','apply',{'operation':'attach','revision':wan['revision'],
                                      'expected_boot_id':wan['dp']['boot_id']})
                network=rpc('network')
            except (ValueError,RuntimeError) as error:
                status.fail('ethernet1/1/attachment','pa5200',str(error))
        unavailable={key for key,value in patches.items() if value['mode']!='disabled'
                     and int(key[1:]) not in network.get('backend',{}).get('ports',[])}
        for key in unavailable:
            status.fail('ethernet1/'+key[1:],'pa5200','Physical forwarding attachment is inactive; interface address and management profile were not applied')
        changed={key:value for key,value in patches.items() if key not in unavailable and network['config']['ports'][key]!=value}
        if changed:
            rpc('network','apply',{'revision':network['config']['revision'],'ports':changed})
            network=rpc('network')
        for name,key,enabled in requested:
            if key in unavailable:continue
            desired=patches[key]
            if network['config']['ports'].get(key)!=desired:
                status.fail(name,'pa5200','Dataplane configuration readback mismatch');continue
            status.ok(name+'/dataplane',None,desired,'pa5200','Dataplane settings read back through MP daemon')
            if desired['mode']!='disabled' and int(key[1:]) not in network.get('backend',{}).get('ports',[]):
                status.fail(name,'pa5200','Configuration stored on DP, but physical forwarding attachment is inactive')
        if not status.errors:
            try:
                routes=committed_routes(device,network['config'])
                if routes is not None:
                    if routes!=network['config'].get('routes',[]):
                        rpc('network','apply',{'revision':network['config']['revision'],'routes':routes})
                        network=rpc('network')
                    if network['config'].get('routes',[])!=routes:raise ValueError('DP route readback mismatch')
                    status.ok('virtual-router/default',None,routes,'pa5200','Static routes read back on DP through MP daemon')
            except (ValueError,RuntimeError,sqlite3.Error) as error:
                status.fail('virtual-router/default','pa5200',str(error))


def committed_routes(device,config,db='/var/lib/ffn-ngfw/config-v2.db'):
    """Default-router static routes; legacy SQL is used until XML migration.

    XML candidate-managed is the migration boundary used by the core VR editor.
    Once present, empty XML means deletion and must never resurrect SQL routes.
    """
    parent=device.find('./network/virtual-router')
    if parent is None:return None
    rows=[]
    for vr in parent.findall('entry'):
        if vr.get('name')!='default':raise ValueError('Only the default virtual router is commissioned by this platform config adapter')
        for route in vr.findall('./routing-table/ip/static-route/entry'):
            rows.append({'dst':route.findtext('destination',''),'via':route.findtext('nexthop/ip-address',''),
                         'dev':route.findtext('interface',''),'metric':int(route.findtext('metric','10'))})
    if parent.findtext('ffn-candidate-managed')!='yes':
        from pathlib import Path
        if Path(db).is_file():
            with sqlite3.connect('file:'+db+'?mode=ro',uri=True) as con:
                legacy=con.execute('SELECT r.dest_cidr,r.next_hop,r.dev,r.metric,v.name FROM static_routes r JOIN virtual_routers v ON r.vr_id=v.id').fetchall()
            for dst,via,dev,metric,vr in legacy:
                if vr!='default':raise ValueError('Legacy non-default router requires an explicit DP VRF mapping')
                if dst not in {r['dst'] for r in rows}:rows.append(dict(dst=dst,via=via,dev=dev,metric=metric))
    result=[]
    for row in rows:
        row['dst']=str(ipaddress.ip_network(row['dst'],strict=True))
        match=re.fullmatch(r'ethernet1/([1-9]|1[0-9]|2[0-4])',row['dev'] or '')
        if match:row['dev']='p'+match[1]
        if not row['dev'] and row['via']:
            gateway=ipaddress.ip_address(row['via'])
            matches=[name for name,p in config['ports'].items() if p['mode']=='l3' and any(
                gateway in ipaddress.ip_interface(a).network for a in p.get('addresses',[]))]
            if len(matches)!=1:raise ValueError('Static route gateway needs an unambiguous Layer 3 egress interface')
            row['dev']=matches[0]
        if row['dev'] not in config['ports']:raise ValueError('Static route egress is not mapped to a dataplane port')
        if not row['via']:row.pop('via')
        result.append(row)
    return result
