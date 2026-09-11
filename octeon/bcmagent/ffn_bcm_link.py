"""Fixed, faceplate-only SDK link control. Called under the BCM daemon lock."""
import re
PORTS=(28,13,14,15,16,1,18,19,6,21,22,23,7,11,36,27,10,29,30,31,32,33,34,35)
SPEEDS={1000:'BCM_PORT_ABILITY_1000MB',10000:'BCM_PORT_ABILITY_10GB',
        40000:'BCM_PORT_ABILITY_40GB',100000:'BCM_PORT_ABILITY_100GB'}

def port_number(req):
    port=req.get('port')
    if type(port) is not int or port not in PORTS: raise ValueError('Mapped faceplate port required')
    # Copper PHY control requires a separate external-PHY driver, not the internal SerDes.
    if port in PORTS[:4]: raise ValueError('Copper PHY speed control is not commissioned')
    return port

def status(chip,req):
    port=port_number(req)
    script=['{ int rv; int an; int speed; bcm_port_ability_t a;',
            'rv=bcm_port_autoneg_get(0,%d,&an); if(rv==0) rv=bcm_port_speed_get(0,%d,&speed);'%(port,port),
            'if(rv==0) rv=bcm_port_ability_local_get(0,%d,&a);'%port,
            'printf("FFNLINK %d %d %d\\n",rv,an,speed);']
    for speed,constant in SPEEDS.items():
        script.append('if(rv==0 && (a.speed_full_duplex & %s)) printf("FFNSPEED %d\\n");'%(constant,speed))
    script.append('}')
    text=chip.run('cint\n'+' '.join(script)+'\nexit;')
    found=re.search(r'^\s*FFNLINK (-?\d+) (\d+) (\d+)\s*$',text,re.M)
    if not found or int(found[1]): raise RuntimeError('SDK link status unavailable')
    # Restrict abilities to the board port class; never expose internal HiGig/lane modes.
    allowed=(1000,10000) if PORTS.index(port)<20 else (40000,100000)
    # CINT echoes the script, including printf literals for unsupported modes.
    # Accept complete result lines only, never text inside an echoed command.
    speeds=sorted({int(v) for v in re.findall(r'^\s*FFNSPEED (\d+)\s*$',text,re.M) if int(v) in allowed})
    return {'port':port,'autoneg':bool(int(found[2])),'speed_mbps':int(found[3]),
            'configured_speed':'auto' if int(found[2]) else str(int(found[3])),
            'supported_speeds':speeds}

def apply(chip,req):
    port=port_number(req)
    if set(req)-{'op','port','speed'}: raise ValueError('Unknown link fields')
    speed=req.get('speed')
    before=status(chip,{'port':port})
    if speed!='auto' and speed not in [str(v) for v in before['supported_speeds']]:
        raise ValueError('Speed is not supported by this port')
    if before['configured_speed']==speed: return before
    # Auto keeps the existing advertisement (including pause/FEC); fixed disables AN.
    script='{ int rv; rv=bcm_port_autoneg_set(0,%d,%d); '%(port,1 if speed=='auto' else 0)
    if speed!='auto': script+='if(rv==0) rv=bcm_port_speed_set(0,%d,%d); '%(port,int(speed))
    script+='printf("FFNSET %d\\n",rv); }'
    text=chip.run('cint\n'+script+'\nexit;')
    result=re.search(r'^\s*FFNSET (-?\d+)\s*$',text,re.M)
    if not result or int(result[1]): raise RuntimeError('SDK link change failed; outcome must be reconciled')
    after=status(chip,{'port':port})
    if after['configured_speed']!=speed: raise RuntimeError('SDK link readback mismatch')
    return after
