#!/usr/bin/env python3
"""CP-owned faceplate admin control; no raw ASIC operations accepted from callers."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import sys

PORTS = (28,13,14,15,16,1,18,19,6,21,22,23,7,11,36,27,10,29,30,31,32,33,34,35)
STATE = Path('/etc/ffn/faceplate.json')


def call(request):
    with socket.create_connection(('127.1.1.2',8104),timeout=5) as sock:
        sock.settimeout(70)
        with sock.makefile('rwb') as stream:
            stream.write(json.dumps(request).encode()+b'\n');stream.flush()
            data=stream.readline(1024*1024+1)
    if len(data)>1024*1024: raise ValueError('BCM response too large')
    result=json.loads(data)
    if not result.get('ok'): raise RuntimeError('BCM operation failed; refresh hardware status')
    return result


def save(value):
    STATE.parent.mkdir(parents=True,exist_ok=True)
    temp=STATE.with_suffix('.tmp')
    with temp.open('w') as f:
        json.dump(value,f);f.flush();os.fsync(f.fileno())
    temp.replace(STATE)
    fd=os.open(STATE.parent,os.O_RDONLY|os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def observe():
    physical={p['port']:p for p in call({'op':'port.list'})['ports']}
    ports=[]
    for front,chip in enumerate(PORTS,1):
        p=physical.get(chip)
        ports.append({'port':front,'name':'ethernet1/%d'%front,'bcm_port':chip,
                      'available':p is not None,'enabled':p.get('enabled') if p else None,
                      'link':p.get('link') if p else None,'speed_mbps':p.get('speed_mb') if p else None})
    for p in ports:
        p.update(speed_configuration=False, supported_speeds=[], configured_speed=None)
        if p['available'] and p['port']>4:
            try:
                link=call({'op':'port.link.status','port':p['bcm_port']})
                p.update(speed_configuration=True, supported_speeds=link['supported_speeds'], configured_speed=link['configured_speed'])
            except (RuntimeError,ValueError,KeyError,OSError):
                p['speed_error']='Link control unavailable; BCM link-control update may need activation'
    revision=int(hashlib.sha256(json.dumps([(p['port'],p['available'],p['enabled'],p['configured_speed'],p['supported_speeds']) for p in ports]).encode()).hexdigest()[:12],16)
    return {'revision':revision,'ports':ports,'capabilities':{'admin_state':True,'speed_configuration':any(p['speed_configuration'] for p in ports),
            'link_is_forwarding':False},'saved':json.loads(STATE.read_text()) if STATE.exists() else {'ports':{}}}


def apply(request):
    if (not isinstance(request,dict) or set(request)-{'revision','port','enabled','speed'} or not {'revision','port'}<=set(request) or not {'enabled','speed'}&set(request) or
            type(request['revision']) is not int or type(request['port']) is not int or
            not 1<=request['port']<=24 or ('enabled' in request and type(request['enabled']) is not bool)):
        raise ValueError('expected revision, faceplate port 1..24 and boolean enabled')
    before=observe()
    if request['revision']!=before['revision']: raise ValueError('revision conflict; refresh ports')
    port=before['ports'][request['port']-1]
    if not port['available']: raise ValueError('port unavailable')
    if 'speed' in request and (not port.get('speed_configuration') or request['speed'] not in ['auto']+[str(v) for v in port['supported_speeds']]):
        raise ValueError('Requested speed unavailable for this port')
    saved=before['saved']
    if saved.get('pending'): raise ValueError('previous operation unresolved; inspect hardware before retry')
    saved['pending']=request
    save(saved)
    if 'speed' in request: call({'op':'port.link.set','port':port['bcm_port'],'speed':request['speed']})
    if 'enabled' in request: call({'op':'port.set','port':port['bcm_port'],'enable':request['enabled']})
    after=observe()
    actual=after['ports'][request['port']-1]
    if ('enabled' in request and actual['enabled'] != request['enabled']) or ('speed' in request and actual['configured_speed'] != request['speed']):
        raise RuntimeError('administrative state readback did not match; operation pending')
    if 'enabled' in request: saved['ports'][str(request['port'])]=request['enabled']
    if 'speed' in request: saved.setdefault('speeds',{})[str(request['port'])]=request['speed']
    saved.pop('pending')
    save(saved)
    after['saved']=saved
    return {'activation':'verified','data':after}


def main():
    with open('/run/ffn-faceplate.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        action=sys.argv[1] if len(sys.argv)==2 else 'status'
        if action=='status': result=observe()
        elif action=='set': result=apply(json.load(sys.stdin))
        elif action=='restore':
            state=json.loads(STATE.read_text()) if STATE.exists() else {'ports':{}}
            if state.get('pending'): raise ValueError('unresolved operation prevents automatic restore')
            for port,speed in state.get('speeds',{}).items():
                result=apply({'revision':observe()['revision'],'port':int(port),'speed':speed})
            for port,enabled in state['ports'].items():
                result=apply({'revision':observe()['revision'],'port':int(port),'enabled':enabled})
            result=observe()
        elif action=='resolve':
            # Local administrative recovery explicitly accepts observed state.
            current=observe();state=current['saved']
            pending=state.get('pending')
            if not pending: raise ValueError('no pending operation')
            port=current['ports'][pending['port']-1]
            if not port['available']: raise ValueError('port unavailable')
            if 'enabled' in pending: state['ports'][str(pending['port'])]=port['enabled']
            if 'speed' in pending:
                if port['configured_speed'] is None: raise ValueError('link state unavailable')
                state.setdefault('speeds',{})[str(pending['port'])]=port['configured_speed']
            state.pop('pending');save(state);result=observe()
        else: raise ValueError('usage: status|set|restore')
        print(json.dumps(result))


if __name__=='__main__':
    try: main()
    except (ValueError,RuntimeError,OSError) as e:
        print(json.dumps({'error':str(e)}));sys.exit(2)
