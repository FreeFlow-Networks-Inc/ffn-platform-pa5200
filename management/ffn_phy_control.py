#!/usr/bin/env python3
"""BCM84848 copper PHY driver on CP SMI bus 0; no caller-supplied registers."""
import fcntl,hashlib,json,os,sys,time
from pathlib import Path
from ffn_mdio import Mdio
GATE=Path('/sys/module/ffn_mdioctl/parameters/allow_writes')
STATE=Path('/etc/ffn/phy.json')

def inventory(bus):
    ports=[]
    for phy in range(16,20):
        read=lambda dev,reg:bus.transfer(phy,dev,reg)
        ident=[read(1,2),read(1,3)]
        row={'phy':phy,'bus':0,'id':ident,'identified':ident==[0x600d,0x84f9],
             'faceplate_mapping_verified':phy==17,'interface':'ethernet1/2' if phy==17 else None}
        if row['identified']:
            firmware=read(30,0x400f);reset=bool(read(1,0)&0x8000)
            link=read(30,0x400d);an=bool(read(7,0)&0x1000)
            ads=[read(7,0xffe4),read(7,0xffe9),read(7,0x20)]
            advertised=[speed for speed,value in [(100,ads[0]&0x100),(1000,ads[1]&0x200),(10000,ads[2]&0x1000)] if value]
            row.update(firmware=firmware,ready=firmware not in (0,65535) and not reset,autoneg=an,
                       link=bool(link&0x20),speed_mbps=(10,100,1000,10000)[(link>>3)&3] if link&0x20 else None,
                       advertised_speeds=advertised,advertisement_registers=ads,
                       configured_speed=str(advertised[0]) if an and len(advertised)==1 else 'auto' if an else 'unmanaged',
                       supported_speeds=[100,1000,10000])
        ports.append(row)
    saved=json.loads(STATE.read_text()) if STATE.exists() else {'speeds':{}}
    revision=int(hashlib.sha256(json.dumps([[p.get(k) for k in ('phy','id','firmware','ready','autoneg','advertisement_registers')] for p in ports]+[saved],sort_keys=True).encode()).hexdigest()[:12],16)
    return {'revision':revision,'phys':ports,'saved':saved,'forwarding_verified':False,
            'warning':'PHY speed selection limits auto-negotiation advertisement. MAC synchronization and physical port mapping must also be commissioned.'}

def persist(value):
    STATE.parent.mkdir(parents=True,exist_ok=True);temp=STATE.with_suffix('.tmp')
    with temp.open('w') as stream:json.dump(value,stream);stream.flush();os.fsync(stream.fileno())
    os.replace(temp,STATE)

def apply(bus,request):
    if (set(request)!={'revision','phy','speed'} or type(request['revision']) is not int or
            type(request['phy']) is not int or request['phy'] not in range(16,20) or request['speed'] not in ('auto','100','1000','10000')):
        raise ValueError('Revision, PHY 16..19 and supported speed required')
    before=inventory(bus)
    if request['revision']!=before['revision']:raise ValueError('revision conflict; refresh PHY inventory')
    phy=request['phy'];row=before['phys'][phy-16];saved=before['saved']
    if not row.get('ready'):raise ValueError('Identified BCM84848 with running firmware required')
    if saved.get('pending'):raise ValueError('Previous PHY operation unresolved')
    # Reapplying an identical advertisement must not restart WAN negotiation.
    expected=[bit if request['speed'] in ('auto',speed) else 0
              for bit,speed in ((0x100,'100'),(0x200,'1000'),(0x1000,'10000'))]
    if row['autoneg'] and [v & mask for v,mask in zip(row['advertisement_registers'],(0x1e0,0x700,0x1000))]==expected:
        saved.setdefault('speeds',{})[str(phy)]=request['speed'];persist(saved)
        return {'activation':'verified','scope':'phy-advertisement-only','forwarding_verified':False,'data':inventory(bus)}
    saved['pending']=request;persist(saved)
    previous_gate=GATE.read_text()
    try:
        GATE.write_text('1')
        def handshake():
            end=time.monotonic()+2
            while bus.transfer(phy,30,0x400e)&2:
                if time.monotonic()>=end:raise RuntimeError('PHY firmware busy')
                time.sleep(.001)
        for reg,mask,bit,speed in ((0xffe4,0x1e0,0x100,'100'),(0xffe9,0x700,0x200,'1000'),(0x20,0x1000,0x1000,'10000')):
            handshake();old=bus.transfer(phy,7,reg)
            value=(old&~mask)|(bit if request['speed'] in ('auto',speed) else 0)
            bus.transfer(phy,7,reg,value)
            if bus.transfer(phy,7,reg)!=value:raise RuntimeError('PHY advertisement readback mismatch')
        handshake()
        bus.transfer(phy,7,0,bus.transfer(phy,7,0)|0x1200)
        handshake()
        after=inventory(bus)
        if after['phys'][phy-16]['configured_speed']!=request['speed']:raise RuntimeError('PHY setting readback mismatch')
        saved['speeds'][str(phy)]=request['speed'];saved.pop('pending');persist(saved)
        return {'activation':'verified','scope':'phy-advertisement-only','forwarding_verified':False,'data':inventory(bus)}
    finally:GATE.write_text(previous_gate)

if __name__=='__main__':
    try:
        with open('/run/lock/ffn-copper.lock','w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX);bus=Mdio()
            try:
                action=sys.argv[1] if len(sys.argv)==2 else 'status'
                if action=='set':result=apply(bus,json.load(sys.stdin))
                elif action=='restore':
                    current=inventory(bus)
                    if current['saved'].get('pending'):raise ValueError('Unresolved PHY operation prevents restore')
                    for address,speed in current['saved']['speeds'].items():
                        apply(bus,{'revision':inventory(bus)['revision'],'phy':int(address),'speed':speed})
                    result=inventory(bus)
                elif action=='resolve':
                    current=inventory(bus);saved=current['saved'];pending=saved.get('pending')
                    if not pending:raise ValueError('No pending operation')
                    observed=current['phys'][pending['phy']-16]
                    if not observed.get('ready') or observed['configured_speed']=='unmanaged':raise ValueError('Observed PHY state cannot be accepted')
                    saved['speeds'][str(pending['phy'])]=observed['configured_speed'];saved.pop('pending');persist(saved);result=inventory(bus)
                elif action=='status':result=inventory(bus)
                else:raise ValueError('status|set|restore|resolve required')
            finally:bus.close()
        print(json.dumps(result))
    except Exception as exc:print(json.dumps({'error':str(exc)}));sys.exit(2)
