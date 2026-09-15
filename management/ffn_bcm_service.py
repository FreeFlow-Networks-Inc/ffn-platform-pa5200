#!/usr/bin/env python3
"""CP service lifecycle endpoint; only the fixed BCM unit can be controlled."""
import fcntl,hashlib,json,os,subprocess,sys,uuid
from pathlib import Path
from ffn_faceplate import call
UNIT='ffn-bcmd.service'
STATE=Path('/var/lib/ffn/bcm-service-request.json')

def status():
    result=subprocess.run(['systemctl','show',UNIT,'-p','ActiveState','-p','SubState','-p','MainPID','-p','Result'],capture_output=True,text=True,check=True,timeout=10)
    service=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    pending=json.loads(STATE.read_text()) if STATE.exists() else None
    complete=not pending or (service.get('ActiveState')=='inactive' if pending['operation']=='stop' else
        service.get('ActiveState')=='active' and (pending['operation']=='start' or service.get('MainPID')!=pending['previous_pid']))
    complete=complete or service.get('ActiveState')=='failed'
    chip=None
    if service.get('ActiveState')=='active':
        try: chip=call({'op':'status'})
        except (OSError,ValueError,RuntimeError): pass
    revision=int(hashlib.sha256(json.dumps([service,pending],sort_keys=True).encode()).hexdigest()[:12],16)
    return {'revision':revision,'unit':UNIT,'service':service,'chip':chip,
            'request':pending,'operation_complete':complete,
            'forwarding_verified':False,'configuration_reapply_required':bool(pending and pending['operation']!='stop'),
            'warning':'Restart reinitializes the switch and interrupts all faceplate links. Service active does not prove forwarding or configuration restoration.'}

def apply(payload):
    if (set(payload)!={'revision','operation','acknowledge_link_outage'} or type(payload['revision']) is not int or
            payload['operation'] not in ('start','stop','restart') or payload['acknowledge_link_outage'] is not True):
        raise ValueError('Current revision, fixed operation and acknowledgement of link outage required')
    before=status()
    if payload['revision']!=before['revision']: raise ValueError('revision conflict; refresh service')
    if not before['operation_complete']: raise ValueError('Previous service operation has not completed')
    request={'id':str(uuid.uuid4()),'operation':payload['operation'],'previous_pid':before['service'].get('MainPID')}
    STATE.parent.mkdir(parents=True,exist_ok=True)
    temp=STATE.with_suffix('.tmp')
    with temp.open('w') as stream:
        json.dump(request,stream);stream.flush();os.fsync(stream.fileno())
    os.replace(temp,STATE)
    subprocess.run(['systemctl','--no-block',payload['operation'],UNIT],check=True,timeout=15)
    return {'activation':'pending','request':request,'message':'Service operation submitted; refresh health and recommit configuration after startup.'}

if __name__=='__main__':
    try:
        with open('/run/ffn-faceplate.lock','w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            result=apply(json.load(sys.stdin)) if len(sys.argv)==2 and sys.argv[1]=='set' else status()
            print(json.dumps(result))
    except Exception as exc:
        print(json.dumps({'error':str(exc)}));sys.exit(2)
