#!/usr/bin/env python3
"""Read-only continuous conntrack observations, supervised over MP-owned SSH.

No nftables mutation, lease renewal, NAT allocation or hardware admission.
Any kernel loss, policy change or collector restart invalidates the inventory.
A fresh generation with a complete snapshot is required before readiness.
"""
import json
import os
from pathlib import Path
import select
import socket
import sys
import time
import uuid

sys.path.insert(0,'/usr/local/lib/ffn')
import ffn_session_feed as feed
from ffn_session_events import subscribe,snapshot,receive,EventGap
from session_stream import CAPACITY,Lines,canonical_uuid,send


def policy_context(state,collector,topology=None):
    result=dict(revision=state['revision'],digest=state['digest'],nat_digest=state['nat']['digest'],
                bindings=state['bindings'],collector=collector)
    if topology is not None:
        result['l3']=dict(snapshot_digest=feed.l3.fingerprint(topology),hardware_admission=False)
    return result


def replay(rows,changes):
    def key(row):return (row.get('zone',0),row['id'],json.dumps(row['original'],sort_keys=True))
    live={key(row):row for row in rows if row['token'] is not None}
    for row in changes:
        ident=key(row)
        if row['event']=='end' or row['token'] is None:live.pop(ident,None)
        else:live[ident]=row
    if len(live)>CAPACITY:raise ValueError('Session inventory exceeds stream capacity')
    return list(live.values())


def route_watch():
    source=socket.socket(socket.AF_NETLINK,socket.SOCK_RAW,0)
    try:
        # Link, neighbor, IPv4 address/route/rule notifications. Any change
        # invalidates this conservative observation generation. Never mask
        # ENOBUFS; a lost notification must also require a fresh snapshot.
        source.bind((0,1|4|0x10|0x40|0x80));source.setblocking(False)
        return source
    except BaseException:source.close();raise


def check_routes(source):
    if select.select([source],[],[],0)[0]:
        raise EventGap('Route, neighbor or interface changed; new session snapshot required')


def stream(nonce,emit,*,clock=time.monotonic,stop=lambda:False):
    canonical_uuid(nonce)
    state,collector,rules=feed.context()
    producer=dict(boot_id=collector['boot_id'],pid=os.getpid(),
        process_start=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19],stream_id=str(uuid.uuid4()))
    sequence=0;known=set()
    def event(operation,payload):
        nonlocal sequence
        # Do not publish a row/heartbeat computed across a queued topology
        # change, including changes arriving during a conntrack burst.
        check_routes(routes)
        sequence+=1
        emit(dict(schema=1,nonce=nonce,producer=producer,sequence=sequence,operation=operation,
                  emitted_monotonic=clock(),payload=payload))
    def check_context():
        if feed.runtime.saved()!=state or feed.acknowledgement(feed.runtime.status(),state)!=collector:
            raise ValueError('Applied policy, bindings or Security collector changed')
    # Subscribe before dumping, then replay interleaved changes. Never label a
    # truncated dump or a lost receive queue as a synchronized snapshot.
    with route_watch() as routes,subscribe() as source:
        deadline=clock()+8
        topology=feed.l3.snapshot(deadline)
        rows,changes=snapshot(source,timeout=3,capacity=CAPACITY)
        rows=replay(rows,changes);check_context();check_routes(routes)
        def assess(row):
            value=feed.assess(row,rules,producer['boot_id'])
            value['l3']=feed.l3.plan(value,state['bindings'],topology)
            return value
        event('begin',policy_context(state,collector,topology))
        chunk=[]
        for row in rows:
            if clock()>=deadline:raise EventGap('Session/L3 snapshot exceeded freshness budget')
            assessed=assess(row);known.add(assessed['identity']);chunk.append(assessed)
            if len(chunk)==32:event('snapshot',chunk);chunk=[]
        if chunk:event('snapshot',chunk)
        if clock()>=deadline:raise EventGap('Session/L3 snapshot exceeded freshness budget')
        check_context();check_routes(routes);event('synchronized',{})
        heartbeat=clock()
        while not stop():
            now=clock()
            if now-heartbeat>=1:
                check_context();event('heartbeat',{});heartbeat=clock()
            readable=select.select([source,routes],[],[],.1)[0]
            if routes in readable:check_routes(routes)
            if source not in readable:continue
            # Bound each burst so traffic cannot starve context checks.
            deadline=clock()+.05;count=0
            while count<2048 and clock()<deadline:
                try:batch=receive(source)
                except BlockingIOError:break
                count+=len(batch)
                for row in batch:
                    # Closed/unowned sessions need only their identity; do
                    # not resolve next hops on the withdrawal path.
                    assessed=feed.assess(row,rules,producer['boot_id']);ident=assessed['identity']
                    if row['event']=='end' or row['token'] is None:
                        if ident in known:event('close',{'identity':ident});known.remove(ident)
                    else:
                        assessed['l3']=feed.l3.plan(assessed,state['bindings'],topology)
                        known.add(ident)
                        if len(known)>CAPACITY:raise EventGap('Session stream capacity exceeded')
                        event('upsert',assessed)


if __name__=='__main__':
    try:
        request=Lines(0).read()
        if set(request)!={'nonce'}:raise ValueError('Stream start accepts only a nonce')
        canonical_uuid(request['nonce'])
        while True:
            try:stream(request['nonce'],lambda value:send(1,value))
            except (BrokenPipeError,TimeoutError):raise
            except Exception as error:
                # A blocked policy or invalidated generation is useful live
                # telemetry, not permission to reuse the previous snapshot.
                send(1,dict(schema=1,nonce=request['nonce'],unavailable=str(error)[:512] or 'DP context unavailable'))
                time.sleep(3)
    except (Exception,KeyboardInterrupt) as error:
        print(str(error)[:512],file=sys.stderr);raise SystemExit(2)
