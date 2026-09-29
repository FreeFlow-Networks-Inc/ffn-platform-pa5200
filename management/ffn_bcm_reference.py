#!/usr/bin/env python3
"""Read bounded BCM88375 SDK evidence through the existing serialized daemon.

OpenBCM sdk-6.5.27 include/bcm/{info,trunk,l3,stat}.h and src/bcm/dpp
are API references, not proof that an API works in the installed vendor SDK.
No port, VLAN, route, trunk, counter reset, or global switch setting is written.
"""
import json
import re
import sys
import time

COUNTERS=('snmpIfHCInOctets','snmpIfHCOutOctets','snmpIfHCInUcastPkts',
          'snmpIfHCOutUcastPkts','snmpIfInErrors','snmpIfOutErrors')
PROBES={
 'device':('bcm_info_t x; bcm_info_t_init(&x); rv=bcm_info_get(0,&x);',
           ('vendor','device','revision'),('x.vendor','x.device','x.revision')),
 'trunk':('bcm_trunk_chip_info_t x; rv=bcm_trunk_chip_info_get(0,&x);',
          ('groups','id_min','id_max','members_max'),
          ('x.trunk_group_count','x.trunk_id_min','x.trunk_id_max','x.trunk_ports_max')),
 'l3':('bcm_l3_info_t x; bcm_l3_info_t_init(&x); rv=bcm_l3_info(0,&x);',
       ('interfaces_max','interfaces_used','hosts_max','hosts_used','routes_max','routes_used'),
       ('x.l3info_max_intf','x.l3info_used_intf','x.l3info_max_host','x.l3info_used_host',
        'x.l3info_max_route','x.l3info_used_route')),
}


def execute(script):
    from ffn_aggregate_hardware import SCRIPT,acquire,call
    with open('/run/ffn-forward-test.lock','a') as lock:
        acquire(lock);previous=SCRIPT.read_bytes()
        try:
            SCRIPT.write_text(script)
            result=call({'op':'cint.run','script':SCRIPT.name,'timeout':10})
        finally:SCRIPT.write_bytes(previous)
    if not result.get('ok') or not result.get('completed') or result.get('truncated'):
        raise RuntimeError('SDK reference query incomplete')
    return result.get('markers',[])


def probe(name,run=execute):
    body,fields,values=PROBES[name]
    script=('{ int rv; '+body+' printf("FFN_REF rv=%d",rv); if(rv==0) printf(" '+
            ' '.join('%d' for _ in fields)+'",'+','.join(values)+
            '); printf("\\nFFN_REF_DONE\\n"); }')
    lines=run(script)
    if lines[-1:]!=['FFN_REF_DONE'] or len(lines)!=2:raise RuntimeError('Incomplete SDK reference readback')
    match=re.fullmatch(r'FFN_REF rv=(-?\d+)((?: -?\d+)*)',lines[0])
    if not match:raise RuntimeError('Invalid SDK reference readback')
    rv=int(match[1]);numbers=[int(v) for v in match[2].split()]
    if len(numbers)!=(0 if rv else len(fields)):raise RuntimeError('Incomplete SDK capacity readback')
    result=dict(available=rv==0,sdk_return=rv,values=dict(zip(fields,numbers)))
    if name=='l3' and rv==0:result['capacity_reported']=any(numbers[::2])
    return result


def counters(ports,run=execute):
    # IDs must originate in port.list; this interface accepts no C expressions.
    if (not isinstance(ports,list) or not 1<=len(ports)<=8 or
        any(type(p) is not int or not 0<=p<=255 for p in ports) or len(set(ports))!=len(ports)):
        raise ValueError('One to eight distinct SDK port IDs required')
    body=['{ int rv; uint64 value;']
    for port in ports:
        for index,counter in enumerate(COUNTERS):
            body.append('COMPILER_64_ZERO(value); rv=bcm_stat_get(0,%d,%s,&value); '
                        'printf("FFN_STAT %d %d %%d %%08x%%08x\\n",rv,COMPILER_64_HI(value),COMPILER_64_LO(value));'
                        %(port,counter,port,index))
    body.append('printf("FFN_REF_DONE\\n"); }')
    lines=run('\n'.join(body));found={}
    if lines[-1:]!=['FFN_REF_DONE']:raise RuntimeError('Missing counter completion')
    for line in lines[:-1]:
        match=re.fullmatch(r'FFN_STAT (\d+) (\d+) (-?\d+) ([0-9a-fA-F]{16})',line)
        if not match:raise RuntimeError('Invalid SDK counter readback')
        port,index,rv=map(int,match.groups()[:3]);key=(port,index)
        if port not in ports or not 0<=index<len(COUNTERS) or key in found:
            raise RuntimeError('Unexpected or duplicate SDK counter')
        found[key]=dict(available=rv==0,sdk_return=rv,value=str(int(match[4],16)) if rv==0 else None)
    if len(found)!=len(ports)*len(COUNTERS):raise RuntimeError('Incomplete SDK counters')
    # Decimal strings preserve all 64 bits when passed through JavaScript.
    return {str(p):{name:found[p,i] for i,name in enumerate(COUNTERS)} for p in ports}


def status():
    from ffn_faceplate import call
    chip=call({'op':'status'})
    if chip.get('state')!='ready':raise RuntimeError('BCM SDK is not ready')
    result=dict(source='live-bcm-sdk',observed_monotonic=time.monotonic(),chip=chip.get('chip'),
                hardware_forwarding_verified=False,features={})
    for name in PROBES:
        try:result['features'][name]=probe(name)
        except (ValueError,OSError,RuntimeError) as error:
            result['features'][name]=dict(available=False,error=str(error))
    from ffn_faceplate import observe
    ports=observe()['ports']
    active=[p['bcm_port'] for p in ports if p.get('enabled') and p.get('link') and type(p.get('bcm_port')) is int]
    result['interfaces']={str(p['bcm_port']):p['name'] for p in ports if p.get('bcm_port') in active}
    result['counters']={}
    for offset in range(0,len(active),8):
        result['counters'].update(counters(active[offset:offset+8]))
    return result


if __name__=='__main__':
    try:
        if len(sys.argv)!=1:raise ValueError('Only read-only SDK status is supported')
        print(json.dumps(status()))
    except Exception as error:
        print(json.dumps({'error':str(error)}));raise SystemExit(2)
