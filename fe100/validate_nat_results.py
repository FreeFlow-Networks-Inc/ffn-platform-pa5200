#!/usr/bin/env python3
"""Audit isolated NAT packet reports; never authorize production admission."""
import argparse
import hashlib
import itertools
import json
from pathlib import Path
from ffn_fe100_clocks import SHA
from validate_front_sessions import expected_return,qualifies_front

CASES=set(itertools.product(('udp','tcp'),('address','port'),(5,13)))


def audit(report):
    protocol=report.get('protocol','udp');mode=report['nat_mode'];ingress=report['ingress']
    case=(protocol,mode,ingress)
    if case not in CASES or report['egress']!=18-ingress:raise ValueError('Unsupported isolated NAT case')
    if (report.get('error') or report.get('cleanup_errors') or report.get('nat_direction_verified') is not True or
        report.get('production_nat_qualified') is not False or report.get('session_offload_verified') is not True or
        report.get('cp_cleanup',{}).get('restored') is not True or report.get('baseline_cleanup',{}).get('restored') is not True):
        raise ValueError('Unverified NAT result or cleanup')
    phases=report['phases']
    if set(phases)!={'baseline','miss','hit','drop','removed'} or any(p['count']!=4 for p in phases.values()) or not qualifies_front(phases):
        raise ValueError('Incomplete packet/counter qualification')
    for name,phase in phases.items():
        fields=('capture_drops',) if name=='baseline' else ('capture_drops','dp_capture_drops')
        if any(type(phase.get(key)) is not int or phase[key]!=0 for key in fields):
            raise ValueError('Missing or lossy packet capture evidence')
    hit=phases['hit'];expected=expected_return(hit['token'],4,ingress==5,mode,protocol)
    actual=[]
    for packet in hit['packets']:
        raw=bytes.fromhex(packet['raw'])
        if len(raw)<32 or (int.from_bytes(raw[24:28],'big')>>22)&63!=ingress:
            raise ValueError('Wrong physical return metadata')
        actual.append(raw[32:])
    # Recompute full IP/transport checksums and expected NAT bytes instead of
    # trusting stored rewritten-index labels in an otherwise successful report.
    if sorted(actual)!=sorted(expected):raise ValueError('NAT bytes/checksums differ from expected packets')
    hardware=report['controller']['hardware']
    epoch=report['baseline_cleanup']['baseline']['epoch']
    if hardware['owner_sha256']!=SHA or not epoch.startswith(hardware['cp_boot_id']+':'):
        raise ValueError('Hardware lifetime or owner ABI mismatch')
    return dict(protocol=protocol,translation=mode,ingress=ingress,egress=18-ingress,packets=4,
                cp_boot_id=hardware['cp_boot_id'],bcm_epoch=epoch,production_owners=report['production_owners'])


def summarize(reports):
    rows=[audit(r) for r in reports];seen=set();identity=None
    for row in rows:
        case=(row['protocol'],row['translation'],row['ingress'])
        if case in seen:raise ValueError('Duplicate qualification case')
        seen.add(case)
        lifetime=(row['cp_boot_id'],row['bcm_epoch'],row['production_owners'])
        if identity is not None and lifetime!=identity:raise ValueError('Qualification crosses hardware or production owner lifetimes')
        identity=lifetime
    return dict(schema=1,scope='isolated IPv4 NAT packet rewrite; sequential directional tests',
                complete=seen==CASES,cases=rows,missing=[list(c) for c in sorted(CASES-seen)],
                production_admission=False,tcp_state_tracking_verified=False,
                simultaneous_bidirectional_verified=False,aggregate_transit_verified=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('reports',nargs='+',type=Path);args=parser.parse_args()
    raw=[p.read_bytes() for p in args.reports]
    result=summarize([json.loads(data) for data in raw])
    result['sources']=[dict(path=str(p),sha256=hashlib.sha256(data).hexdigest()) for p,data in zip(args.reports,raw)]
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
