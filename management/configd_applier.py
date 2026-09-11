"""Apply committed PA-5200 ethernet configuration through the MP owner daemon."""
import json
import re
import subprocess
import uuid
from xml.etree import ElementTree as ET


def rpc(resource, action='status', payload=None):
    request={'v':1,'id':str(uuid.uuid4()),'resource':resource,'action':action,'payload':payload or {}}
    result=subprocess.run(['/usr/bin/python3','/usr/local/lib/ffn/ffn_planed.py','call',
                           '--socket','/run/ffn-plane-mp/control.sock'],input=json.dumps(request),
                          capture_output=True,text=True,timeout=125,check=True)
    response=json.loads(result.stdout)
    if not response.get('ok'):
        raise ValueError('MP request %s: %s (%s)'%(request['id'],response.get('state'),response.get('error')))
    return response['result']


class PlatformApplier:
    def __init__(self, config): self.config=config

    def claims(self, xpath):
        return '.network.interface.ethernet.' in xpath or '.network.interface.aggregate-ethernet.' in xpath

    def reconcile(self, status):
        root=ET.parse(self.config).getroot()
        device=root.find("./devices/entry[@name='localhost.localdomain']")
        if device is None: return
        faceplate=rpc('faceplate')
        network=rpc('network')
        patches={}
        requested=[]
        for entry in device.findall('./network/interface/ethernet/entry'):
            name=entry.get('name','');match=re.fullmatch(r'ethernet1/([1-9]|1[0-9]|2[0-4])',name)
            if not match:
                status.fail(name,'pa5200','Unmapped faceplate interface');continue
            port=int(match[1]);key='p%d'%port
            if entry.find('aggregate-group') is not None:
                status.fail(name,'pa5200','Hardware aggregate/LACP membership is not supported by the commissioned backend');continue
            state=entry.findtext('link-state','auto')
            if state not in ('up','down','auto'):
                status.fail(name,'pa5200','Unsupported link-state');continue
            enabled=state!='down'
            observed=next((p for p in faceplate['ports'] if p['port']==port),None)
            if not observed or not observed['available']:
                status.fail(name,'pa5200','Faceplate port unavailable');continue
            if observed['enabled']!=enabled:
                answer=rpc('faceplate','apply',{'revision':faceplate['revision'],'port':port,'enabled':enabled})
                faceplate=answer['data']
            status.ok(name+'/link-state',None,state,'pa5200','Physical administrative state verified through MP daemon')
            if key not in network['config']['ports']:
                status.fail(name,'pa5200','No commissioned dataplane attachment for this port');continue
            unsupported=[child.tag for child in entry if child.tag not in ('comment','link-state','layer3')]
            l3=entry.find('layer3')
            if unsupported or l3 is None or any(c.tag not in ('ip','mtu') for c in l3):
                status.fail(name,'pa5200','Interface mode or option is not implemented by this config adapter');continue
            desired={'mode':'l3','addresses':[e.get('name') for e in l3.findall('./ip/entry')]} if enabled else {'mode':'disabled'}
            if l3.findtext('mtu'): desired['mtu']=int(l3.findtext('mtu'))
            patches[key]=desired;requested.append((name,key,enabled))
        for entry in device.findall('./network/interface/aggregate-ethernet/entry'):
            status.fail(entry.get('name','aggregate'),'pa5200','Aggregate/LACP configuration is not supported on this backend')
        changed={key:value for key,value in patches.items() if network['config']['ports'][key]!=value}
        if changed:
            rpc('network','apply',{'revision':network['config']['revision'],'ports':changed})
            network=rpc('network')
        for name,key,enabled in requested:
            desired=patches[key]
            if network['config']['ports'].get(key)!=desired:
                status.fail(name,'pa5200','Dataplane configuration readback mismatch');continue
            status.ok(name+'/dataplane',None,desired,'pa5200','Dataplane settings read back through MP daemon')
            if enabled and int(key[1:]) not in network.get('backend',{}).get('ports',[]):
                status.fail(name,'pa5200','Configuration stored on DP, but physical forwarding attachment is inactive')
