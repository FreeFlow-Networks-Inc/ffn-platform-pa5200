#!/usr/bin/env python3
"""Supervise internal MAC loopback on the two disabled FE100 lab ports.

This is a commissioning fixture, never a production port configuration.
EOF, SIGTERM or 90 seconds without a heartbeat restores the disabled baseline.
Every mutation is journaled first; an incomplete prior run requires recovery.
"""
import json
from pathlib import Path
import re
import select
import signal
import sys
import time
from ffn_fe100_bcm_lab import execute, locked, save

STATE = Path('/var/lib/ffn/fe100/mac-lab.json')
PORTS = (7, 16)


def recipe(port, operation='status'):
    if port not in PORTS or operation not in ('status', 'begin', 'restore'):
        raise ValueError('Unsupported MAC lab operation')
    change = ''
    if operation == 'begin':
        change = ('if(en!=0 || lb!=0)rv=-8;'
                  'if(rv==0)rv=bcm_port_loopback_set(0,p,BCM_PORT_LOOPBACK_MAC);'
                  'if(rv==0)rv=bcm_port_enable_set(0,p,1);')
    elif operation == 'restore':
        change = ('if(lb!=0 && lb!=BCM_PORT_LOOPBACK_MAC)rv=-8;'
                  'if(rv==0)rv=bcm_port_enable_set(0,p,0);'
                  'if(rv==0)rv=bcm_port_loopback_set(0,p,BCM_PORT_LOOPBACK_NONE);')
    return '''{int p=%d;int rv=0;int en=0;int lb=0;int speed=0;int link=0;
 if(rv==0)rv=bcm_port_enable_get(0,p,&en);
 if(rv==0)rv=bcm_port_loopback_get(0,p,&lb);
 %s
 if(rv==0)rv=bcm_port_enable_get(0,p,&en);
 if(rv==0)rv=bcm_port_loopback_get(0,p,&lb);
 if(rv==0)rv=bcm_port_speed_get(0,p,&speed);
 if(rv==0)rv=bcm_port_link_status_get(0,p,&link);
 printf("FFN_MAC_LAB port=%%d enabled=%%d loopback=%%d speed=%%d link=%%d rv=%%d\\n",p,en,lb,speed,link,rv);
 if(rv==0)printf("FFN_DONE\\n");}
''' % (port, change)


class Owner:
    def __init__(self, call, epoch, ports=PORTS):
        self.call, self.epoch = call, epoch
        if not ports or len(set(ports))!=len(ports) or any(p not in PORTS for p in ports):
            raise ValueError('Invalid MAC lab ports')
        self.ports=tuple(ports)
        self.record = None

    def query(self, port, operation='status'):
        with locked(Path('/run/ffn-faceplate.lock')):
            result = execute(recipe(port, operation), self.call)
        rows = [re.fullmatch(r'FFN_MAC_LAB port=(\d+) enabled=([01]) loopback=(\d+) speed=(\d+) link=([01]) rv=0', s)
                for s in result.get('markers', [])]
        rows = [dict(zip(('port','enabled','loopback','speed','link'), map(int, m.groups()))) for m in rows if m]
        if len(rows) != 1 or rows[0]['port'] != port:
            raise RuntimeError('MAC lab state readback failed')
        return rows[0]

    def persist(self):
        save(self.record, STATE)

    def begin(self):
        if STATE.exists() and json.loads(STATE.read_text()).get('stage') != 'restored':
            raise RuntimeError('Pending MAC lab recovery')
        before = [self.query(p) for p in self.ports]
        if any(p['enabled'] or p['loopback'] or p['link'] for p in before):
            raise RuntimeError('MAC lab requires disabled, unlinked, non-looped ports')
        self.record = dict(epoch=self.epoch(), stage='preparing', before=before, touched=[])
        self.persist()
        for port in self.ports:
            self.record['touched'].append(port); self.persist()
            current = self.query(port, 'begin')
            if current['enabled'] != 1 or current['loopback'] != 1:
                raise RuntimeError('MAC loopback did not apply')
        self.record['stage'] = 'active'; self.persist()
        return self.status()

    def status(self):
        if self.record is None or self.record['epoch'] != self.epoch():
            raise RuntimeError('MAC lab hardware lifetime changed')
        rows = [self.query(p) for p in self.ports]
        if any(p['enabled'] != 1 or p['loopback'] != 1 for p in rows):
            raise RuntimeError('MAC lab port ownership changed')
        return dict(ready=True, ports=rows, external_wire_verified=False)

    def restore(self):
        if self.record is None: return
        if self.record['epoch'] != self.epoch():
            raise RuntimeError('Refusing stale MAC lab cleanup after BCM restart')
        errors = []
        for port in reversed(self.record['touched']):
            try:
                row = self.query(port, 'restore')
                before = next(p for p in self.record['before'] if p['port'] == port)
                if any(row[k] != before[k] for k in ('enabled','loopback','speed')):
                    raise RuntimeError('MAC lab baseline differs after restore')
            except Exception as error: errors.append(str(error))
        self.record.update(stage='recovery_required' if errors else 'restored', cleanup_errors=errors)
        self.persist()
        if errors: raise RuntimeError('; '.join(errors))


def main():
    from ffn_faceplate import call
    from ffn_copper_forwarding import epoch
    args=sys.argv[1:];ports=PORTS
    if args in (['--serve','--port','5'],['--serve','--port','13']):
        ports=(16 if args[-1]=='5' else 7,);args=['--serve']
    if args not in (['--serve'], ['--recover']): raise ValueError('Use --serve [--port 5|13] or --recover')
    def terminated(*_): raise RuntimeError('MAC lab supervisor terminated')
    signal.signal(signal.SIGTERM, terminated)
    with locked(Path('/run/ffn-fe100-mac-lab.lock')):
        owner = Owner(call, epoch, ports)
        if args == ['--recover']:
            if STATE.exists(): owner.record = json.loads(STATE.read_text()); owner.restore()
            print(json.dumps(dict(restored=True))); return
        try:
            print(json.dumps(owner.begin()), flush=True)
            while select.select([sys.stdin], [], [], 90)[0]:
                line = sys.stdin.readline()
                if not line: break
                if json.loads(line) == {'op':'finish'}: break
                if json.loads(line) != {'op':'status'}: raise ValueError('Invalid MAC lab command')
                print(json.dumps(owner.status()), flush=True)
        finally:
            owner.restore()
            print(json.dumps(dict(restored=True)), flush=True)


if __name__ == '__main__': main()
