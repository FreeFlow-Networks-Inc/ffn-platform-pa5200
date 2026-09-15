#!/usr/bin/env python3
"""Follow commissioned copper PHY rates; all user intent comes from MP configuration."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time
import ffn_faceplate as face
import ffn_phy_control as phy
STATUS=Path('/run/ffn-copper-link.json')
JOURNAL=Path('/var/lib/ffn/copper-sync-pending.json')

def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix('.tmp')
    with tmp.open('w') as stream:json.dump(value,stream);stream.flush();os.fsync(stream.fileno())
    os.replace(tmp,path)
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)

def matches(mac,speed):
    return mac['enabled'] and mac['speed_mbps']==speed and not mac['autoneg'] and mac['full_duplex'] and mac['interface']==('xfi' if speed==10000 else 'sgmii')

def reconcile(bus):
    result={'ports':{},'forwarding_verified':False,'checked_at':time.time()}
    if JOURNAL.exists():
        pending=json.loads(JOURNAL.read_text())
        actual=face.call({'op':'port.copper.status','port':pending['bcm_port']})
        if matches(actual,pending['speed_mbps']):
            JOURNAL.unlink()
        else:
            result.update(state='blocked',pending=pending);save(STATUS,result);return result
    if face.call({'op':'status'}).get('state')!='ready':
        result['state']='waiting-for-bcm';save(STATUS,result);return result
    saved=json.loads(face.STATE.read_text()) if face.STATE.exists() else {}
    if saved.get('pending'):
        result['state']='faceplate-operation-pending';save(STATUS,result);return result
    inventory=phy.inventory(bus)
    if inventory['saved'].get('pending'):
        result['state']='phy-operation-pending';save(STATUS,result);return result
    previous=json.loads(STATUS.read_text()) if STATUS.exists() else {}
    for row in inventory['phys']:
        name=row.get('interface')
        if not name or row.get('bcm_port') not in (28,13,14,15):continue
        port=row['bcm_port'];speed=row.get('speed_mbps')
        observed={'phy':row['phy'],'speed_mbps':speed,'link':row.get('link',False)}
        result['ports'][name]=observed
        if not row.get('ready') or not row.get('enabled') or not row.get('link') or speed not in (100,1000,10000):
            observed['state']='waiting-for-phy';continue
        mac=face.call({'op':'port.copper.status','port':port});observed['mac']=mac
        if not mac['enabled']:
            observed['state']='mac-disabled';continue
        if matches(mac,speed):observed['state']='synchronized';continue
        last=previous.get('ports',{}).get(name,{})
        if last.get('phy')!=row['phy'] or last.get('speed_mbps')!=speed or not last.get('link'):
            observed['state']='waiting-for-stable-rate';continue
        # Mapping/configuration is locked across this read and the ASIC mutation.
        # No optical ports, PHY advertisements or administrative intents are changed.
        pending={'interface':name,'phy':row['phy'],'bcm_port':port,'speed_mbps':speed,'before':mac}
        save(JOURNAL,pending)
        observed['mac']=face.call({'op':'port.copper.sync','port':port,'speed_mbps':speed})
        if not matches(observed['mac'],speed):raise RuntimeError('MAC synchronization readback mismatch')
        JOURNAL.unlink()
        fd=os.open(JOURNAL.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
        observed['state']='synchronized'
    result['state']='observed';save(STATUS,result);return result

def main():
    # Initialization owns MAC rates until its oneshot service has finished.
    if subprocess.run(['systemctl','is-active','--quiet','ffn-front-ports.service']).returncode:
        save(STATUS,{'state':'waiting-for-front-ports','ports':{},'forwarding_verified':False});return
    with open('/run/ffn-faceplate.lock','w') as lock,open('/run/lock/ffn-copper.lock','w') as phylock:
        fcntl.flock(lock,fcntl.LOCK_EX);fcntl.flock(phylock,fcntl.LOCK_EX)
        bus=phy.Mdio()
        try:reconcile(bus)
        finally:bus.close()

if __name__=='__main__':
    try:main()
    except Exception as exc:
        save(STATUS,{'state':'error','error':str(exc),'pending':JOURNAL.exists(),'ports':{},'forwarding_verified':False})
        raise
