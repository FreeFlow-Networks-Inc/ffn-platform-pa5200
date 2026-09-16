"""Authenticated FFN-CLI commands; execution remains in the MP control daemon."""
import json
import shlex
import uuid


def handle(line, api, token):
    parts=shlex.split(line)
    if len(parts)<2 or parts[:2] not in (['show','platform'],['request','platform']): return False
    if parts==['show','platform','aggregates']:
        result=api('/api/interfaces/aggregate-status',token=token)
        print(json.dumps(result,indent=2));return True
    if parts==['show','platform','wan-path'] or (len(parts)==4 and parts[:3]==['request','platform','wan-path']):
        def plane(action,payload):
            ident=str(uuid.uuid4())
            try:
                value=api('/api/system/planes',method='POST',token=token,
                    body={'v':1,'id':ident,'resource':'wan-path','action':action,'payload':payload})
            except Exception as error:
                raise ValueError('WAN outcome uncertain; request '+ident+': '+str(error)) from error
            if not value.get('ok'):raise ValueError('WAN operation failed; request '+ident+': '+str(value.get('error')))
            return value['result']
        if parts[0]=='request' and parts[3] not in ('probe','recover','attach','detach'):raise ValueError('WAN operation must be probe, recover, attach or detach')
        result=plane('status',{})
        if parts[0]=='request':
            result=plane('apply',{'revision':result['revision'],'operation':parts[3],'expected_boot_id':result['dp']['boot_id']})
        print(json.dumps(result,indent=2));return True
    if parts[:2]==['show','platform'] and len(parts) in (2,3):
        resource=parts[2] if len(parts)==3 else 'status'
        if resource in ('control', 'agents', 'fe100', 'control-events'):
            result=api('/api/system/control' + ('/events' if resource=='control-events' else ''),token=token)
            if resource=='fe100':
                result={name: {'fresh': state['fresh'], 'age_seconds':state['age_seconds'],
                    'fe100':(state.get('last_observation') or {}).get('report',{}).get('fe100')}
                    for name,state in result['agents'].items() if state['role']=='cp'}
            print(json.dumps(result,indent=2))
            return True
        if resource not in ('status','bcm','phy','faceplate','dataplane','network','inspection','overlay','chassis','thermal','fabric'):
            raise ValueError('unknown platform resource')
        result=api('/api/system/runtime/'+resource,token=token)
    elif parts[:3]==['request','platform','bcm'] and len(parts)==5 and parts[3] in ('start','stop','restart') and parts[4]=='acknowledge-link-outage':
        observed=api('/api/system/runtime/bcm',token=token)
        result=api('/api/system/runtime/bcm/set',method='POST',token=token,body={'revision':observed['revision'],'operation':parts[3],'acknowledge_link_outage':True})
    elif parts[:3]==['request','platform','interface'] and len(parts)==5 and parts[4] in ('renegotiate','recover-pairs'):
        import re
        match=re.fullmatch(r'ethernet1/([1-4])',parts[3])
        if not match:raise ValueError('Mapped copper ethernet1/1..4 interface required')
        observed=api('/api/system/runtime/faceplate',token=token)
        field='restart_autoneg' if parts[4]=='renegotiate' else 'restore_pair_map'
        result=api('/api/system/runtime/faceplate/set',method='POST',token=token,
                   body={'revision':observed['revision'],'port':int(match[1]),field:True})
    elif parts[:3]==['request','platform','interface'] and len(parts)==6 and parts[4]=='link-speed':
        import re
        match=re.fullmatch(r'ethernet1/([1-9]|1[0-9]|2[0-4])',parts[3])
        if not match: raise ValueError('Mapped ethernet1/1..24 interface required')
        observed=api('/api/system/runtime/faceplate',token=token)
        result=api('/api/system/runtime/faceplate/set',method='POST',token=token,
                   body={'revision':observed['revision'],'port':int(match[1]),'speed':parts[5]})
    elif parts[:2]==['request','platform'] and len(parts)==5:
        resource,action=parts[2:4]
        if (resource,action) not in {('phy','set'),('bcm','set'),('faceplate','set'),('network','patch'),('network','lookup'),('inspection','set'),('overlay','set'),('thermal','auto'),('thermal','full')}:
            raise ValueError('unknown platform operation')
        result=api('/api/system/runtime/'+resource+'/'+action,method='POST',body=json.loads(parts[4]),token=token)
    else:
        raise ValueError('usage: show platform [resource] | request platform RESOURCE ACTION JSON')
    print(json.dumps(result,indent=2))
    return True
