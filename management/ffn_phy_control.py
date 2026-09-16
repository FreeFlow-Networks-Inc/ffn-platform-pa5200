#!/usr/bin/env python3
"""BCM84848 copper PHY driver on CP SMI bus 0; no caller-supplied registers."""
import fcntl,hashlib,json,os,sys,time
from pathlib import Path
from ffn_mdio import Mdio
GATE=Path('/sys/module/ffn_mdioctl/parameters/allow_writes')
STATE=Path('/etc/ffn/phy.json')
MAPPING=Path('/etc/ffn/copper-map.json')

def port_mapping():
    # Commissioned associations override vendor numbering on this appliance.
    # Require both the panel-to-PHY and PHY-to-MAC association for MAC writes.
    mapping=json.loads(MAPPING.read_text()) if MAPPING.exists() else {}
    if not isinstance(mapping,dict):raise ValueError('Copper mapping object required')
    result={}
    for front,value in mapping.items():
        # Legacy PHY-only maps remain readable but do not authorize MAC writes.
        entry={'phy':value,'bcm_port':None} if type(value) is int else value
        if (front not in ('1','2','3','4') or not isinstance(entry,dict) or set(entry)!={'phy','bcm_port'}
                or type(entry['phy']) is not int or entry['phy'] not in range(16,20)
                or (entry['bcm_port'] is not None and (type(entry['bcm_port']) is not int or entry['bcm_port'] not in (28,13,14,15)))):
            raise ValueError('Invalid copper PHY/MAC mapping')
        result[front]=entry
    phys=[v['phy'] for v in result.values()];macs=[v['bcm_port'] for v in result.values() if v['bcm_port'] is not None]
    if len(set(phys))!=len(phys) or len(set(macs))!=len(macs):raise ValueError('Ambiguous copper mapping')
    return result


def inventory(bus):
    ports=[];mapping=port_mapping()
    for phy in range(16,20):
        read=lambda dev,reg:bus.transfer(phy,dev,reg)
        ident=[read(1,2),read(1,3)]
        row={'phy':phy,'bus':0,'id':ident,'identified':ident==[0x600d,0x84f9],
             'faceplate_mapping_verified':any(v['phy']==phy for v in mapping.values()),
             'bcm_port':next((v['bcm_port'] for v in mapping.values() if v['phy']==phy),None),
             'interface':next(('ethernet1/'+p for p,v in mapping.items() if v['phy']==phy),None)}
        if row['identified']:
            firmware=read(30,0x400f);reset=bool(read(1,0)&0x8000)
            link=read(30,0x400d)
            controls=[read(7,0xffe0),read(7,0)]
            an=all(value!=0xffff and value&0x1000 for value in controls)
            # Both status registers latch link loss; use the second observation.
            for reg in (0xffe1,1):read(7,reg)
            status=[read(7,0xffe1),read(7,1)]
            partner=[read(7,0xffe5),read(7,0xffea),read(7,0x21)]
            control=read(30,0x401a)
            ads=[read(7,0xffe4),read(7,0xffe9),read(7,0x20)]
            advertised=[speed for speed,value in [(100,ads[0]&0x100),(1000,ads[1]&0x200),(10000,ads[2]&0x1000)] if value]
            row.update(enabled=not bool(control&0x8080),control_register=control,firmware=firmware,ready=firmware not in (0,65535) and not reset,autoneg=an,
                       link=bool(link&0x20),speed_mbps=(10,100,1000,10000)[(link>>3)&3] if link&0x20 else None,
                       advertised_speeds=advertised,advertisement_registers=ads,
                       configured_speed=str(advertised[0]) if an and len(advertised)==1 else 'auto' if an else 'unmanaged',
                       supported_speeds=[100,1000,10000])
            row['negotiation']={'enabled_1000':controls[0]!=0xffff and bool(controls[0]&0x1000),
                'enabled_10000':controls[1]!=0xffff and bool(controls[1]&0x1000),
                'complete_1000':status[0]!=0xffff and bool(status[0]&0x20),
                'complete_10000':status[1]!=0xffff and bool(status[1]&0x20),
                'partner_registers':partner,'valid':all(v!=0xffff for v in controls+status+partner)}
            row['autoneg_controls']=[v&~0x200 for v in controls]
            row['pair_map_register']=read(30,0x4009)
            row['pair_map_recovery']=row['ready'] and firmware==0x1089 and row['enabled'] and not row['link']
        ports.append(row)
    saved=json.loads(STATE.read_text()) if STATE.exists() else {'speeds':{}}
    revision=int(hashlib.sha256(json.dumps([[p.get(k) for k in ('phy','id','firmware','ready','autoneg','autoneg_controls','advertisement_registers','control_register','interface','bcm_port')] for p in ports]+[saved],sort_keys=True).encode()).hexdigest()[:12],16)
    return {'revision':revision,'phys':ports,'saved':saved,'forwarding_verified':False,
            'warning':'PHY speed selection limits auto-negotiation advertisement. MAC synchronization and physical port mapping must also be commissioned.'}

def persist(value):
    STATE.parent.mkdir(parents=True,exist_ok=True);temp=STATE.with_suffix('.tmp')
    with temp.open('w') as stream:json.dump(value,stream);stream.flush();os.fsync(stream.fileno())
    os.replace(temp,STATE)

def apply(bus,request):
    if (not isinstance(request,dict) or set(request)-{'revision','phy','speed','enabled','restart_autoneg','restore_pair_map'} or not {'revision','phy'}<=set(request)
            or not {'speed','enabled','restart_autoneg','restore_pair_map'}&set(request) or type(request['revision']) is not int
            or type(request['phy']) is not int or request['phy'] not in range(16,20)
            or ('speed' in request and request['speed'] not in ('auto','100','1000','10000'))
            or ('enabled' in request and type(request['enabled']) is not bool)
            or any(key in request and (request[key] is not True or set(request)!={'revision','phy',key}) for key in ('restart_autoneg','restore_pair_map'))):
        raise ValueError('Revision, PHY 16..19 and supported speed or boolean enabled required')
    before=inventory(bus)
    if request['revision']!=before['revision']:raise ValueError('revision conflict; refresh PHY inventory')
    phy=request['phy'];row=before['phys'][phy-16];saved=before['saved']
    if not row.get('ready'):raise ValueError('Identified BCM84848 with running firmware required')
    if saved.get('pending'):raise ValueError('Previous PHY operation unresolved')
    if row['control_register']&0x8000:raise ValueError('PHY is super-isolated; recover firmware before applying port configuration')
    recover=request.get('restore_pair_map',False)
    if recover and (not row.get('pair_map_recovery') or not row.get('faceplate_mapping_verified') or row['bcm_port'] is None
                    or row['pair_map_register']==0xffff):
        raise ValueError('Pair mapping recovery requires a mapped, enabled, down PHY with firmware 0x1089')
    restart=request.get('restart_autoneg',False) or recover
    if restart and (not row['enabled'] or not row['negotiation']['valid'] or not row['advertised_speeds']):
        raise ValueError('Enabled PHY with valid negotiation registers and advertisement required')
    masks=(0x1e0,0x700,0x1000)
    expected=[bit if request.get('speed') in ('auto',speed) else 0
              for bit,speed in ((0x100,'100'),(0x200,'1000'),(0x1000,'10000'))]
    speed_change='speed' in request and (not row['autoneg'] or [v&m for v,m in zip(row['advertisement_registers'],masks)]!=expected)
    admin_change='enabled' in request and row['enabled']!=request['enabled']
    def finish():
        if 'speed' in request:saved.setdefault('speeds',{})[str(phy)]=request['speed']
        if 'enabled' in request:saved.setdefault('admin',{})[str(phy)]=request['enabled']
        if recover:saved.setdefault('pair_maps',{})[str(phy)]=0xe4
        saved.pop('pending',None);persist(saved)
        return {'activation':'verified','scope':'phy-control','forwarding_verified':False,'data':inventory(bus)}
    if (speed_change or restart) and not row['negotiation']['valid']:
        raise ValueError('Invalid negotiation register readback')
    if not speed_change and not admin_change and not restart:return finish()
    saved['pending']=dict(request)
    if recover:saved['pending']['pair_map_before']=row['pair_map_register']
    persist(saved)
    previous_gate=GATE.read_text()
    try:
        GATE.write_text('1')
        def handshake():
            end=time.monotonic()+2
            while bus.transfer(phy,30,0x400e)&2:
                if time.monotonic()>=end:raise RuntimeError('PHY firmware busy')
                time.sleep(.001)
        if recover:
            # PA-5220 sysroot bcm_copper_phy_initialize, 0x126102ac..0x12610330.
            # This fixed board recipe is not a caller-selected register interface.
            # The firmware consumes scratch command 0x52; preserve evidence on failure.
            handshake()
            current=bus.transfer(phy,30,0x400d)
            if current==0xffff or current&0x20:raise RuntimeError('PHY link changed before pair mapping recovery')
            bus.transfer(phy,30,0x4005,2);time.sleep(.035)
            handshake();bus.transfer(phy,30,0x4009,0xe4)
            bus.transfer(phy,30,0x4005,0x52);time.sleep(.035);handshake()
            if bus.transfer(phy,30,0x4009)!=0xe4:raise RuntimeError('Pair mapping command data readback mismatch')
        if speed_change:
            for reg,mask,value in zip((0xffe4,0xffe9,0x20),masks,expected):
                handshake();old=bus.transfer(phy,7,reg);value=(old&~mask)|value
                bus.transfer(phy,7,reg,value)
                if bus.transfer(phy,7,reg)!=value:raise RuntimeError('PHY advertisement readback mismatch')
        if speed_change or restart:
            # Sysroot _phy_8481_copper_an_set: legacy MII first, then 10G AN.
            # Preserve unrelated bits; never reset the PHY or reload firmware.
            for reg in (0xffe0,0):
                handshake();old=bus.transfer(phy,7,reg)
                if old==0xffff:raise RuntimeError('Invalid autonegotiation control readback')
                bus.transfer(phy,7,reg,old|0x1200);handshake()
                actual=bus.transfer(phy,7,reg)
                if actual==0xffff or not actual&0x1000:raise RuntimeError('Autonegotiation enable readback mismatch')
        if admin_change:
            # Broadcom phy8481 copper_enable_set: change only XGPH_DISABLE bit 7.
            handshake();value=bus.transfer(phy,30,0x401a)
            value=(value&~0x80) if request['enabled'] else (value|0x80)
            bus.transfer(phy,30,0x401a,value);handshake()
            if bus.transfer(phy,30,0x401a)!=value:raise RuntimeError('PHY admin readback mismatch')
        after=inventory(bus)['phys'][phy-16]
        if ('speed' in request and after['configured_speed']!=request['speed']) or ('enabled' in request and after['enabled']!=request['enabled']):
            raise RuntimeError('PHY setting readback mismatch')
        return finish()
    finally:GATE.write_text(previous_gate)


def restore_pair_maps(bus):
    current=inventory(bus)
    if current['saved'].get('pending'):raise ValueError('Unresolved PHY operation prevents restore')
    saved=current['saved'].get('pair_maps',{})
    if not isinstance(saved,dict) or any(p not in ('16','17','18','19') or type(v) is not int or v!=0xe4 for p,v in saved.items()):
        raise ValueError('Invalid saved board pair mapping')
    for address,value in saved.items():
        current=inventory(bus);row=current['phys'][int(address)-16]
        if not row.get('ready') or row.get('firmware')!=0x1089:raise ValueError('Pair mapping restore requires firmware 0x1089')
        # A warm service restart must not renegotiate a correctly configured WAN.
        if row.get('pair_map_register')==value:continue
        apply(bus,{'revision':current['revision'],'phy':int(address),'restore_pair_map':True})


if __name__=='__main__':
    try:
        with open('/run/lock/ffn-copper.lock','w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX);bus=Mdio()
            try:
                action=sys.argv[1] if len(sys.argv)==2 else 'status'
                if action=='set':result=apply(bus,json.load(sys.stdin))
                elif action=='restore':
                    restore_pair_maps(bus)
                    current=inventory(bus)
                    if current['saved'].get('pending'):raise ValueError('Unresolved PHY operation prevents restore')
                    for address,speed in current['saved'].get('speeds',{}).items():
                        apply(bus,{'revision':inventory(bus)['revision'],'phy':int(address),'speed':speed})
                    for address,enabled in current['saved'].get('admin',{}).items():
                        apply(bus,{'revision':inventory(bus)['revision'],'phy':int(address),'enabled':enabled})
                    result=inventory(bus)
                elif action=='resolve':
                    current=inventory(bus);saved=current['saved'];pending=saved.get('pending')
                    if not pending:raise ValueError('No pending operation')
                    if pending.get('restore_pair_map'):
                        raise ValueError('Pair recovery outcome requires explicit hardware review; generic resolve is disabled')
                    observed=current['phys'][pending['phy']-16]
                    if not observed.get('ready') or observed['configured_speed']=='unmanaged':raise ValueError('Observed PHY state cannot be accepted')
                    if 'speed' in pending:saved.setdefault('speeds',{})[str(pending['phy'])]=observed['configured_speed']
                    if 'enabled' in pending:saved.setdefault('admin',{})[str(pending['phy'])]=observed['enabled']
                    saved.pop('pending');persist(saved);result=inventory(bus)
                elif action=='status':result=inventory(bus)
                else:raise ValueError('status|set|restore|resolve required')
            finally:bus.close()
        print(json.dumps(result))
    except Exception as exc:print(json.dumps({'error':str(exc)}));sys.exit(2)
