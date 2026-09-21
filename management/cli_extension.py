"""FFN-CLI commands using its request adapter; console execution is local controld IPC."""
import json
import shlex
import uuid

FE100_VIEWS = ('status', 'driver', 'counters', 'policy', 'recovery', 'capabilities', 'json')


def help_text():
    return ('show platform fe100 [status|driver|counters|policy|recovery|capabilities] [json]\n'
            '  Read FE100 observations through MP controld; stale data is labelled.\n'
            '  Bare fe100 or fe100 json preserves the complete JSON report.\n'
            'show platform control | agents | control-events\n'
            '  Inspect plane connectivity and recent control events.')


def complete(prefix, text):
    """Local command vocabulary only; completion never requests hardware data."""
    try: parts = shlex.split(prefix)
    except ValueError: return []
    if parts == ['show']: choices = ['platform']
    elif parts == ['show', 'platform']:
        choices = ['fe100', 'control', 'agents', 'control-events', 'aggregates', 'mp-interfaces',
                   'wan-path', 'status', 'bcm', 'phy', 'faceplate', 'dataplane', 'network',
                   'inspection', 'overlay', 'chassis', 'thermal', 'fabric']
    elif parts == ['show', 'platform', 'fe100']: choices = list(FE100_VIEWS)
    elif len(parts) == 4 and parts[:3] == ['show', 'platform', 'fe100']:
        choices = ['json'] if parts[3] in FE100_VIEWS[:-1] else []
    elif parts in (['help'], ['?']): choices = ['platform']
    elif parts in (['help', 'platform'], ['?', 'platform']): choices = ['fe100']
    else: return None
    return [value for value in choices if value.startswith(text)]


def _lines(value, prefix=''):
    if isinstance(value, dict):
        for key, child in value.items(): yield from _lines(child, prefix + str(key) + '.')
    elif isinstance(value, list):
        for index, child in enumerate(value): yield from _lines(child, prefix + str(index) + '.')
        if not value: yield prefix.rstrip('.') + ': none'
    else:
        rendered = 'unknown' if value is None else str(value)
        # Agent errors/names are data, never terminal control sequences.
        rendered = ''.join(c if c.isprintable() else ' ' for c in rendered)
        label = ''.join(c if c.isprintable() else ' ' for c in prefix.rstrip('.'))
        yield label + ': ' + rendered


def show_fe100(parts, api, token):
    tail = parts[3:]
    as_json = not tail or tail[-1:] == ['json']
    if tail[-1:] == ['json']: tail = tail[:-1]
    if len(tail) > 1 or (tail and tail[0] not in FE100_VIEWS[:-1]):
        raise ValueError('usage: show platform fe100 [status|driver|counters|policy|recovery|capabilities] [json]')
    view = tail[0] if tail else None
    if view=='capabilities':
        response=api('/api/system/planes',method='POST',token=token,
                     body={'v':1,'id':str(uuid.uuid4()),'resource':'fe100-policy','action':'status','payload':{}})
        result=response.get('result') or {}
        if response.get('ok') is not True or not isinstance(result.get('capabilities'),dict):
            raise RuntimeError('Current FE100 capability observation is unavailable')
        values=result['capabilities']
        if as_json:print(json.dumps(values,indent=2))
        else:
            print('FE100 implementation capabilities (not a hardware activation acknowledgement)')
            for line in _lines(values):print('  '+line)
        return True
    states = api('/api/system/control', token=token).get('agents', {})
    result = {}
    for name, state in states.items():
        if state.get('role') != 'cp': continue
        report = (state.get('last_observation') or {}).get('report') or {}
        driver, fe100, policy = (report.get(key) or {} for key in ('fe100_driver','fe100','policy'))
        fresh = state.get('fresh') is True
        recovery = policy.get('recovery') or {}
        details = {'fe100': report.get('fe100'), 'driver': report.get('fe100_driver'),
                   'policy': report.get('policy')}
        if view == 'status':
            details = {'status': {
                'register_access': 'verified' if fresh and driver.get('userspace',{}).get('read_verified') is True else 'unverified',
                'policy_phase': policy.get('configured_phase'),
                'policy_revision': policy.get('configured_revision'),
                'journaled_sessions': policy.get('journaled_sessions'),
                'recovery_outcome': recovery.get('outcome'),
                'drain_verified': fresh and recovery.get('drain_verified') is True,
                'hardware_activation_verified': fresh and policy.get('hardware_activation_verified') is True,
                'forwarding_verified': fresh and fe100.get('offload_verified') is True}}
        elif view:
            details = {view: {'driver': report.get('fe100_driver'), 'policy': report.get('policy'),
                             'counters': fe100.get('counters'), 'recovery': policy.get('recovery')}[view]}
        result[name] = {'fresh': fresh, 'age_seconds': state.get('age_seconds'), **details}
    if as_json:
        print(json.dumps(result, indent=2))
    elif not result:
        print('FE100 observations unavailable: no control-plane agent reported.')
    else:
        for name, observation in result.items():
            print('FE100 ' + ''.join(c if c.isprintable() else ' ' for c in name))
            if not observation['fresh']: print('STALE / UNAVAILABLE: values below are historical observations.')
            for line in _lines(observation): print('  ' + line)
    return True


def handle(line, api, token):
    parts=shlex.split(line)
    if parts in (['help','platform'], ['?','platform'], ['help','platform','fe100'], ['?','platform','fe100']):
        print(help_text()); return True
    if len(parts)<2 or parts[:2] not in (['show','platform'],['request','platform']): return False
    if parts[:3] == ['show','platform','fe100']:
        return show_fe100(parts, api, token)
    if parts==['show','platform','mp-interfaces']:
        print(json.dumps(api('/api/system/mp-interfaces',token=token),indent=2));return True
    if parts[:3]==['request','platform','mp-interface'] and len(parts)==5:
        from urllib.parse import quote
        current=api('/api/system/mp-interfaces',token=token)
        port=next((p for p in current['ports'] if p['name']==parts[3]),None)
        if port is None:raise ValueError('Detected external management interface required')
        result=api('/api/system/mp-interfaces/'+quote(parts[3],safe=''),method='PUT',token=token,
            body={'revision':port['revision'],'config':json.loads(parts[4])})
        print(json.dumps(result,indent=2));return True
    if parts==['show','platform','aggregates']:
        result=api('/api/interfaces/aggregate-status',token=token)
        print(json.dumps(result,indent=2));return True
    if parts[:3]==['request','platform','aggregate'] and len(parts)==5:
        import re
        name,operation=parts[3:]
        if not re.fullmatch(r'ae(?:[1-9]|1[0-2])',name) or operation not in ('activate','offload','negotiate','deactivate','recover'):
            raise ValueError('usage: request platform aggregate aeN activate|offload|negotiate|deactivate|recover')
        observed=api('/api/interfaces/aggregate-status',token=token)
        payload={'group':name,'operation':operation,'running_revision':observed['running_revision'],'revision':observed['revision']}
        result=api('/api/system/planes',method='POST',token=token,
            body={'v':1,'id':str(uuid.uuid4()),'resource':'aggregates','action':'apply','payload':payload})
        if not result.get('ok'):raise ValueError('Aggregate operation failed: '+str(result.get('error')))
        print(json.dumps(result['result'],indent=2));return True
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
        if resource in ('control', 'agents', 'control-events'):
            result=api('/api/system/control' + ('/events' if resource=='control-events' else ''),token=token)
            print(json.dumps(result,indent=2))
            return True
        if resource not in ('status','bcm','phy','faceplate','dataplane','network','inspection','overlay','chassis','thermal','fabric'):
            raise ValueError('unknown platform resource')
        result=api('/api/system/runtime/'+resource,token=token)
    elif parts[:3]==['request','platform','bcm'] and len(parts)==5 and parts[3] in ('start','stop','restart') and parts[4]=='acknowledge-link-outage':
        observed=api('/api/system/runtime/bcm',token=token)
        result=api('/api/system/runtime/bcm/set',method='POST',token=token,body={'revision':observed['revision'],'operation':parts[3],'acknowledge_link_outage':True})
    elif parts[:3]==['request','platform','interface'] and len(parts)==6 and parts[4]=='mode':
        import re
        from urllib.parse import quote
        if not re.fullmatch(r'ethernet1/([1-9]|1[0-9]|2[0-4])',parts[3]) or parts[5] not in ('none','default','off','disabled'):
            raise ValueError('usage: request platform interface ethernet1/N mode none')
        configured=api('/api/interfaces/configured',token=token)
        current=next((row for row in configured['ethernet'] if row['name']==parts[3]),{})
        body={'name':parts[3],'mode':'none'}
        for key in ('comment','link_speed','link_duplex','link_state'):
            if key in current:body[key]=current[key]
        result=api('/api/interfaces/'+quote(parts[3],safe=''),method='PUT',token=token,body=body)
        result=dict(result,staged=True,requires_commit=True)
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
