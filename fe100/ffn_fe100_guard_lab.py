#!/usr/bin/env python3
"""Supervised isolated FE100 physical-session commissioning on the CP.

The MP harness must own the isolated port pair, reject configured interfaces,
and restore link settings independently. This process never enables links.
The selected environment profile must return on its first front port. No
production admission, punt-path changes or automated boot activation occurs.
"""
import argparse
import json
import os
from pathlib import Path
import re
import select
import signal
import sys
import time
from ffn_fe100_guard import Lease, publish, supervise
from ffn_fe100_lab_guard import Recovery


def serve(path, fault_injection=False):
    from ffn_fe100_packet_lab import Lab, PORT_PAIR, FRONT_RETURN
    from ffn_copper_forwarding import epoch
    import ffn_fe100_bcm_lab as bcm
    lease=Lease()
    lab=Lab()
    record=dict(schema=1,cleanup_owner='guardian',bcm_epoch=epoch(),
                lab_journal=str(lab.path),front_ports=PORT_PAIR,stage='starting',
                rules=[],cleanup_errors=[])
    save=lambda:publish(path,record)
    raw_call=lab.call
    def call(*args,**kwargs):
        lease.pulse();result=raw_call(*args,**kwargs);lease.pulse();return result
    lab.call=call
    def route(mode):
        lease.pulse()
        result=bcm.run(dict(mode=mode,front_ports=PORT_PAIR))
        lease.pulse();return result
    try:
        save()
        health=lab.call('readiness')
        if health['commissioning_blockers']:raise RuntimeError(health['commissioning_blockers'])
        if any(health['registers'][k] for k in ('0x40428','0x40450')):
            raise RuntimeError('isolated commissioning requires empty flow tables')
        print(json.dumps(dict(ready=True,journal=str(path),health=health)),flush=True)
        last_command=time.monotonic()
        while time.monotonic()-last_command<60:
            if not select.select([sys.stdin],[],[],1)[0]:lease.pulse();continue
            line=sys.stdin.readline()
            if not line:break
            last_command=time.monotonic();op=json.loads(line)['op']
            if op=='finish':break
            if fault_injection and op=='guard-crash':os._exit(99)
            if fault_injection and op=='guard-stall':os.kill(os.getpid(),signal.SIGSTOP)
            if op=='prepare':
                if record.get('rule_pending') or record['rules']:
                    raise RuntimeError('physical path already prepared or uncertain')
                lab.command('prepare')
                record['rule_pending']=True;save()
                result=route('dsa-front5-create')
                ids={k:int(v) for k,v in re.findall(
                    r'\b(group|entry|stat|dq1|dq2|presel|trap)=(-?\d+)', '\n'.join(result['markers']))}
                if not {'group','entry','dq1','dq2'}<=ids.keys():
                    raise RuntimeError('BCM allocation identity missing')
                record['rules'].append(ids);record['rule_pending']=False;save()
                record['redirect_touched']=True;save()
                route('front5-session-enable')
                result=lab.command('snapshot')
            elif op in ('install','drop','remove','snapshot'):result=lab.command(op)
            else:raise ValueError('unsupported supervised lab operation')
            print(json.dumps(result),flush=True)
    finally:
        # Only the parent may withdraw hardware. On owner death its cgroup
        # also contains all native workers holding the inherited table lock.
        lab.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal',required=True)
    parser.add_argument('--fault-injection',action='store_true')
    parser.add_argument('--owned',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    from ffn_fe100_packet_lab import (Lab, ROOT, PORT_PAIR, FRONT_RETURN,
                                     VLAN_RETURN, MAC_LOOPBACK, IPV6_MISS_LAB)
    from ffn_copper_forwarding import epoch
    import ffn_fe100_bcm_lab as bcm
    path=Path(args.journal)
    if (path.is_symlink() or path.resolve().parent!=ROOT.resolve() or
            not path.name.startswith('guarded-physical-') or path.suffix!='.json'):
        raise ValueError('physical journal must be a guarded-physical JSON file in the FE100 state directory')
    if FRONT_RETURN!=PORT_PAIR[0] or VLAN_RETURN or MAC_LOOPBACK or IPV6_MISS_LAB:
        raise ValueError('unsupported supervised physical profile')
    if args.owned:
        serve(path,args.fault_injection);return
    def route(mode,ids=None):
        return bcm.run(dict(mode=mode,ids=ids or {},front_ports=PORT_PAIR))
    recovery=Recovery(path,epoch=epoch,lab_factory=Lab,route=route)
    command=[sys.executable,'-u',str(Path(__file__).resolve()),'--journal',str(path),'--owned']
    if args.fault_injection:command.append('--fault-injection')
    state=supervise(command,recovery,path.with_suffix('.guard.json'),timeout=25,startup_timeout=45)
    print(json.dumps(dict(restored=state.get('withdrawal_acknowledged') is True,
                         journal=str(path),guard=state,errors=[])),flush=True)


if __name__=='__main__':main()
