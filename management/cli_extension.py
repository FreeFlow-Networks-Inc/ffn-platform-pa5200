"""Authenticated FFN-CLI commands; execution remains in the MP control daemon."""
import json
import shlex


def handle(line, api, token):
    parts=shlex.split(line)
    if len(parts)<2 or parts[:2] not in (['show','platform'],['request','platform']): return False
    if parts[:2]==['show','platform'] and len(parts) in (2,3):
        resource=parts[2] if len(parts)==3 else 'status'
        if resource not in ('status','faceplate','dataplane','network','inspection','overlay','chassis','thermal','fabric'):
            raise ValueError('unknown platform resource')
        result=api('/api/system/runtime/'+resource,token=token)
    elif parts[:2]==['request','platform'] and len(parts)==5:
        resource,action=parts[2:4]
        if (resource,action) not in {('faceplate','set'),('network','patch'),('network','lookup'),('inspection','set'),('overlay','set'),('thermal','auto'),('thermal','full')}:
            raise ValueError('unknown platform operation')
        result=api('/api/system/runtime/'+resource+'/'+action,method='POST',body=json.loads(parts[4]),token=token)
    else:
        raise ValueError('usage: show platform [resource] | request platform RESOURCE ACTION JSON')
    print(json.dumps(result,indent=2))
    return True
