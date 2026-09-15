"""Fixed SDK operations for the four copper-facing switch MACs only."""
import re
PORTS=(28,13,14,15)

def port_number(req):
    port=req.get('port')
    if type(port) is not int or port not in PORTS:raise ValueError('Copper MAC port required')
    return port

def status(chip,req):
    port=port_number(req)
    script=('{ int rv=0; int en=0; int speed=0; int an=0; int duplex=0; bcm_port_if_t intf; '
            'rv=bcm_port_enable_get(0,%d,&en); '
            'if(rv==0) rv=bcm_port_speed_get(0,%d,&speed); '
            'if(rv==0) rv=bcm_port_autoneg_get(0,%d,&an); '
            'if(rv==0) rv=bcm_port_interface_get(0,%d,&intf); '
            'if(rv==0) rv=bcm_port_duplex_get(0,%d,&duplex); '
            'printf("FFNCOPPER %%d %%d %%d %%d %%d %%d %%d\n",rv,en,speed,an,duplex,intf==BCM_PORT_IF_SGMII,intf==BCM_PORT_IF_XFI); }')%((port,)*5)
    text=chip.run('cint\n'+script+'\nexit;')
    found=re.search(r'^\s*FFNCOPPER (-?\d+) (\d+) (\d+) (\d+) (\d+) (\d+) (\d+)\s*$',text,re.M)
    if not found or int(found[1]):raise RuntimeError('Copper MAC status unavailable')
    return {'port':port,'enabled':bool(int(found[2])),'speed_mbps':int(found[3]),
            'autoneg':bool(int(found[4])),'full_duplex':bool(int(found[5])),
            'interface':'sgmii' if int(found[6]) else 'xfi' if int(found[7]) else 'other'}

def apply(chip,req):
    port=port_number(req);speed=req.get('speed_mbps')
    if set(req)-{'op','port','speed_mbps'} or type(speed) is not int or speed not in (100,1000,10000):
        raise ValueError('Negotiated copper rate 100, 1000 or 10000 required')
    before=status(chip,req)
    mode='xfi' if speed==10000 else 'sgmii'
    if before['speed_mbps']==speed and before['interface']==mode and before['full_duplex'] and not before['autoneg']:return before
    if not before['enabled']:raise ValueError('Disabled MAC must not be reconfigured by link synchronization')
    interface='BCM_PORT_IF_XFI' if speed==10000 else 'BCM_PORT_IF_SGMII'
    # The external PHY owns copper AN. Its host-facing MAC follows the result.
    script=('{ int rv; int restore; rv=bcm_port_enable_set(0,%d,0); '
            'if(rv==0) rv=bcm_port_autoneg_set(0,%d,0); '
            'if(rv==0) rv=bcm_port_interface_set(0,%d,%s); '
            'if(rv==0) rv=bcm_port_speed_set(0,%d,%d); '
            'if(rv==0) rv=bcm_port_duplex_set(0,%d,1); '
            'restore=bcm_port_enable_set(0,%d,1); '
            'printf("FFNCOPPERSET %%d %%d\n",rv,restore); }')%(port,port,port,interface,port,speed,port,port)
    text=chip.run('cint\n'+script+'\nexit;')
    result=re.search(r'^\s*FFNCOPPERSET (-?\d+) (-?\d+)\s*$',text,re.M)
    if not result or int(result[1]) or int(result[2]):raise RuntimeError('Copper MAC change failed; inspect pending synchronization')
    after=status(chip,req)
    if not after['enabled'] or after['speed_mbps']!=speed or after['interface']!=mode or after['autoneg'] or not after['full_duplex']:
        raise RuntimeError('Copper MAC readback mismatch')
    return after
