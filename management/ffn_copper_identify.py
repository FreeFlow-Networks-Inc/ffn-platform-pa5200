#!/usr/bin/env python3
"""CP-owned, link-edge identification of the four copper panel ports.

No register writes or guessed panel numbering. The operator selects a panel
port before connecting a test peer. A unique, stable new PHY link identifies
that port; the board's vendor PHY-to-MAC wiring supplies its switch endpoint.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid
import ffn_phy_control as phy
import ffn_faceplate as face

# Board wiring from octeon/bcmagent/ffn-bcm-overrides.conf. This is NOT a
# panel-number map. In particular, PHY17/BCM28 is confirmed panel port2.
PHY_MAC = {16:13, 17:28, 18:15, 19:14}
PROBE = Path('/run/ffn-copper-identify.json')
BOOT = Path('/proc/sys/kernel/random/boot_id')
TTL = 900


def atomic(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.identify-new')
    with temporary.open('w') as stream:
        os.fchmod(stream.fileno(),0o600)
        json.dump(data,stream);stream.flush();os.fsync(stream.fileno())
    temporary.replace(path)
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


class Identifier:
    def __init__(self,bus,clock=time.monotonic,sleep=time.sleep):
        self.bus,self.clock,self.sleep=bus,clock,sleep

    def snapshot(self):
        data=phy.inventory(self.bus)
        mapping=phy.port_mapping()
        rows=data['phys']
        if len(rows)!=4 or {r['phy'] for r in rows}!={16,17,18,19}:
            raise ValueError('all four copper PHYs must be identified')
        if any(not r.get('ready') or not r.get('identified') or r.get('control_register',0)&0x8000 for r in rows):
            raise ValueError('all four copper PHYs must have running firmware')
        if data['saved'].get('pending') or (face.STATE.exists() and json.loads(face.STATE.read_text()).get('pending')):
            raise ValueError('resolve pending PHY/faceplate changes first')
        revision=int(hashlib.sha256(json.dumps([mapping,data['revision']],sort_keys=True).encode()).hexdigest()[:12],16)
        return {'config':{'revision':revision,'mapping':mapping},'phys':rows}

    def probe(self):
        if not PROBE.exists():return None
        saved=json.loads(PROBE.read_text())
        if saved['boot_id']!=BOOT.read_text().strip() or not 0<=self.clock()-saved['started']<TTL:
            return None
        return saved

    def candidate(self,snapshot,probe):
        if snapshot['config']['revision']!=probe['revision']:
            raise ValueError('hardware configuration changed; cancel and start identification again')
        before=probe['links'];after={str(r['phy']):r['link'] for r in snapshot['phys']}
        changes=[int(p) for p in before if before[p]!=after[p]]
        if len(changes)!=1 or after[str(changes[0])] is not True or before[str(changes[0])] is not False:
            raise ValueError('connect one test link to the selected port; exactly one new PHY link is required')
        address=changes[0]
        if any(entry['phy']==address for entry in snapshot['config']['mapping'].values()):
            raise ValueError('the new link belongs to an already mapped port')
        row=next(r for r in snapshot['phys'] if r['phy']==address)
        if not row.get('enabled') or row.get('speed_mbps') not in (100,1000,10000):
            raise ValueError('test PHY must be enabled with a supported negotiated speed')
        return address

    def status(self):
        observed=self.snapshot();probe=self.probe()
        mapping=observed['config']['mapping']
        observed.update(pending=None,candidate=None,complete=len(mapping)==4 and all(v['bcm_port'] is not None for v in mapping.values()))
        if probe:
            observed['pending']={k:probe[k] for k in ('port','token')}
            observed['pending']['seconds_remaining']=max(0,int(TTL-(self.clock()-probe['started'])))
            try:
                address=self.candidate(observed,probe)
                observed['candidate']={'phy':address,'bcm_port':PHY_MAC[address]}
            except ValueError as e:observed['waiting_reason']=str(e)
        return observed

    def execute(self,request,write=False):
        if not isinstance(request,dict):raise ValueError('identification request required')
        op=request.get('operation')
        fields={'operation','revision','port'} if op=='begin' else {'operation','revision','token'}
        if op not in ('begin','confirm','cancel') or set(request)!=fields or type(request['revision']) is not int:
            raise ValueError('invalid identification operation or fields')
        current=self.snapshot();probe=self.probe()
        if current['config']['revision']!=request['revision']:raise ValueError('revision conflict; refresh identification status')
        if op=='begin':
            port=request['port']
            if type(port) is not int or port not in range(1,5):raise ValueError('select copper panel port 1 through 4')
            if str(port) in current['config']['mapping']:raise ValueError('an existing physical mapping cannot be overwritten')
            if probe:raise ValueError('finish or cancel the current identification first')
            if write:
                atomic(PROBE,{'port':port,'token':str(uuid.uuid4()),'started':self.clock(),
                              'boot_id':BOOT.read_text().strip(),'revision':request['revision'],
                              'links':{str(r['phy']):r['link'] for r in current['phys']}})
        else:
            if not probe or request['token']!=probe['token']:raise ValueError('identification expired or token changed')
            if op=='confirm':
                address=self.candidate(current,probe)
                self.sleep(.3)
                if self.candidate(self.snapshot(),probe)!=address:raise ValueError('test link is not stable')
                mac=PHY_MAC[address]
                available=face.call({'op':'port.list'})['ports']
                if not any(p['port']==mac for p in available):raise ValueError('matching copper MAC is unavailable')
                if any(entry.get('bcm_port')==mac for entry in current['config']['mapping'].values()):
                    raise ValueError('matching copper MAC already belongs to another port')
                if write:
                    mapping=dict(current['config']['mapping'])
                    mapping[str(probe['port'])]={'phy':address,'bcm_port':mac}
                    atomic(phy.MAPPING.with_name('copper-map.previous.json'),current['config']['mapping'])
                    atomic(phy.MAPPING,mapping)
            if write:PROBE.unlink()
        return self.status() if write else {'validated':True}


def main():
    if len(sys.argv)!=2 or sys.argv[1] not in ('status','validate','apply'):raise ValueError('status|validate|apply required')
    raw=sys.stdin.buffer.read(65537)
    if len(raw)>65536:raise ValueError('request too large')
    request=json.loads(raw) if raw.strip() else {}
    with open('/run/ffn-faceplate.lock','a') as lock,open('/run/lock/ffn-copper.lock','a') as phylock:
        fcntl.flock(lock,fcntl.LOCK_EX);fcntl.flock(phylock,fcntl.LOCK_EX)
        bus=phy.Mdio()
        try:
            owner=Identifier(bus)
            if sys.argv[1]=='status':
                if request:raise ValueError('status takes no payload')
                result=owner.status()
            else:result=owner.execute(request,write=sys.argv[1]=='apply')
        finally:bus.close()
    print(json.dumps(result))


if __name__=='__main__':
    try:main()
    except Exception as e:print(json.dumps({'error':str(e)[:512]}));sys.exit(2)
