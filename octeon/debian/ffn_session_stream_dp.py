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
from session_stream import BATCH,CAPACITY,Lines,canonical_uuid,send

# Seconds a snapshot may take from subscription to 'synchronized': the L3 and conntrack dumps plus one
# Python assessment per session. 1,801 sessions on the PA-5220 (2026-10-14) took longer than the former 8 s,
# so every attempt failed and the producer re-dumped the table continuously. The relay allows 90 s of
# emission lag (session_relay.STREAM_BACKLOG); silence between chunks stays bounded by the relay's 10 s.
SNAPSHOT_BUDGET=60
# Rows per snapshot chunk: each chunk is one relay round trip through the CP, and the round trips are what
# a snapshot's wall time is made of (2,300 sessions took 28 s in 64-row chunks on 2026-10-21). The receivers
# accept up to BATCH rows per message.
CHUNK=BATCH
# Seconds a generation outlives a topology change that touched no next hop any emitted plan forwards
# through. LAN hosts' neighbour entries come and go every few tens of seconds on a busy box; ending the
# generation for each one kept the feed re-snapshotting most of the day (2026-10-21). A plan that would use
# a changed neighbour is withheld until the deferred end, so no plan ever carries a stale next hop.
DEFERRED_SECONDS=900
# Seconds between counter refreshes. Conntrack reports a session's packet and
# byte counts in dumps and in the events that create or destroy it, not while
# an established flow forwards, so without a refresh a download's row keeps the
# counts from its handshake for its whole life: the offload policy cannot see
# which flows are fat and a hardware lease cannot see that its flow still moves.
# A dump every ten seconds, with counter-only upserts for the candidate rows
# whose counts moved, is the cheapest honest signal.
REFRESH_SECONDS=10
CONNTRACK_MAX=Path('/proc/sys/net/netfilter/nf_conntrack_max')


def policy_context(state,collector,topology=None,limits=None):
    result=dict(revision=state['revision'],digest=state['digest'],nat_digest=state['nat']['digest'],
                bindings=state['bindings'],collector=collector)
    if topology is not None:
        result['l3']=dict(snapshot_digest=feed.l3.fingerprint(topology),hardware_admission=False)
    if limits is not None:result['limits']=limits
    return result


def table_limits(path=None):
    """The kernel's session-table bound beside this stream's own capacity; omitted when unreadable."""
    try:maximum=int(Path(CONNTRACK_MAX if path is None else path).read_text().strip())
    except (OSError,ValueError):return None
    if not 0<=maximum<2**31:return None
    return dict(conntrack_max=maximum,stream_capacity=CAPACITY)


def coalesce(changes):
    """One entry per identity, the latest winning, in first-appearance order: a burst that creates,
    confirms and establishes the same flow costs the receivers one upsert."""
    ordered={}
    for entry in changes:
        identity=entry['identity'] if entry['op']=='close' else entry['row']['identity']
        ordered[identity]=entry
    return list(ordered.values())


def batches(changes):
    """The coalesced changes in events of at most BATCH entries: the receivers refuse a larger batch."""
    merged=coalesce(changes)
    return [merged[start:start+BATCH] for start in range(0,len(merged),BATCH)]


def with_known_owner(row,ident,known):
    """The owner an event speaks for.

    Conntrack labels travel in dumps and in the events that changed them; a later
    update (TCP state, assured, counters) carries none, which the decoder reports as
    token None. That is silence about ownership, not a withdrawal: the owner already
    known for the identity stands. An unknown identity without labels stays unowned.
    """
    if row['event']=='end' or row['token'] is not None or ident not in known:return row
    return dict(row,token=known[ident])


def counters_known(row):
    """Whether a decoded conntrack row carries packet and byte counts for both directions."""
    counters=row.get('counters')
    return (isinstance(counters,dict) and
            all(isinstance(counters.get(d),dict) and {'packets','bytes'}<=set(counters[d]) for d in ('original','reply')))


def identity_of(row,boot):
    """The stream identity of a decoded row: the same digest ffn_session_feed.assess emits."""
    import hashlib
    return hashlib.sha256(json.dumps([boot,row.get('zone',0),row['id'],row['original']],sort_keys=True).encode()).hexdigest()


def refreshed(dumped,emitted,boot):
    """Counter-only upserts for the emitted candidate rows whose dump counters moved.

    `emitted` maps identity to the row last sent for it. A dump row for an unknown
    identity, a non-candidate, or one whose owner or TCP state differs from the
    emitted row is left alone: those arrive through the event path with a full
    assessment and next-hop plan, which this refresh deliberately does not redo.
    """
    updates=[]
    for row in dumped:
        if row.get('event')=='end' or not counters_known(row):continue
        previous=emitted.get(identity_of(row,boot))
        if (previous is None or previous.get('software_candidate') is not True or
                previous.get('token')!=row.get('token') or previous.get('tcp_state')!=row.get('tcp_state') or
                previous.get('counters')==row['counters']):
            continue
        updates.append(dict(previous,counters=row['counters'],remaining_seconds=row.get('timeout')))
    return updates


def replay(rows,changes):
    def key(row):return (row.get('zone',0),row['id'],json.dumps(row['original'],sort_keys=True))
    live={key(row):row for row in rows if row['token'] is not None}
    for row in changes:
        ident=key(row)
        if row['event']=='end':live.pop(ident,None)
        elif row['token'] is None:
            if ident in live:live[ident]=dict(row,token=live[ident]['token'])
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


def drain(source):
    """Consume queued notifications; a lost one (ENOBUFS) is a gap."""
    count=0
    while True:
        try:source.recv(65536)
        except BlockingIOError:return count
        except OSError as error:
            raise EventGap('Notification queue overflowed; new session snapshot required') from error
        count+=1


def neighbor_table(topology):
    """The projected neighbours keyed by (device, address): what a plan's next hop resolves through."""
    return {(n.get('dev'),n.get('dst')):(n.get('lladdr'),n.get('valid')) for n in topology.get('neighbors') or []}


def planned_neighbors(emitted):
    """The (device, next hop) pairs the emitted rows' available plans forward through."""
    used=set()
    for row in emitted.values():
        plan=row.get('l3') or {}
        if plan.get('available') is not True:continue
        for direction in plan.get('directions') or []:used.add((direction.get('device'),direction.get('next_hop')))
    return used


def topology_change(topology,fresh,used=None):
    """What a retaken topology changes for the generation planned on `topology`.

    None: nothing. A frozenset of neighbour keys: only neighbours no emitted plan
    forwards through changed (`used` names those in use), which the generation can
    outlive by withholding any later plan through them. Raises EventGap when a link,
    address, route or rule changed, or a neighbour in use did, or without `used`.
    """
    if fresh==topology:return None
    if used is None or any(fresh.get(k)!=topology.get(k) for k in (fresh.keys()|topology.keys())-{'neighbors'}):
        raise EventGap('Route, neighbor or interface changed; new session snapshot required')
    before,after=neighbor_table(topology),neighbor_table(fresh)
    changed=frozenset(k for k in before.keys()|after.keys() if before.get(k)!=after.get(k))
    if changed&set(used):raise EventGap('Next-hop neighbor changed; new session snapshot required')
    return changed


def check_routes(source,topology=None,deadline=None,observe=None,used=None):
    """Invalidate the generation only when a notification changed what plans use.

    Under forwarded traffic the neighbour state machine notifies every few
    seconds without changing any next hop. The projected topology snapshot
    ignores that churn, so a notification is drained and the snapshot retaken;
    the generation ends only when the retaken topology differs in something
    plans use. Returns False when quiet, True when the retaken topology is the
    same, and the changed neighbour keys when only unused neighbours changed
    (`used` is the in-use set or a callable producing it). Without a reference
    topology any notification is a gap, as before.
    """
    if not select.select([source],[],[],0)[0]:return False
    drain(source)
    if topology is None:
        raise EventGap('Route, neighbor or interface changed; new session snapshot required')
    fresh=(observe or feed.l3.snapshot)(deadline if deadline is not None else time.monotonic()+3)
    if fresh==topology:return True
    return topology_change(topology,fresh,used() if callable(used) else used)


def withheld(plan,changed):
    """A plan through a neighbour that changed after the generation's topology snapshot is withheld."""
    if plan.get('available') is not True or not changed:return plan
    if any((d.get('device'),d.get('next_hop')) in changed for d in plan.get('directions') or []):
        return dict(plan,available=False,directions=[],blockers=['Next-hop neighbor changed after the topology snapshot'])
    return plan


def stream(nonce,emit,*,clock=time.monotonic,stop=lambda:False):
    canonical_uuid(nonce)
    state,collector,rules=feed.context()
    producer=dict(boot_id=collector['boot_id'],pid=os.getpid(),
        process_start=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19],stream_id=str(uuid.uuid4()))
    sequence=0;known={};emitted={};topology=None;changed=frozenset();deferred=None;heartbeat=None
    def recheck():
        # Do not publish a row/heartbeat computed across a topology change,
        # including changes arriving during a conntrack burst. Notifications
        # that left the projected topology unchanged do not end the generation;
        # neighbours no emitted plan uses defer the end and withhold later plans.
        nonlocal changed,deferred
        outcome=check_routes(routes,topology,clock()+3,used=lambda:planned_neighbors(emitted))
        if isinstance(outcome,frozenset) and outcome:
            changed=changed|outcome
            if deferred is None:
                deferred=clock()+DEFERRED_SECONDS
                print('topology changed beside every planned next hop; the generation ends in %d s'%DEFERRED_SECONDS,file=sys.stderr,flush=True)
        if deferred is not None and clock()>=deferred:
            raise EventGap('Deferred route, neighbor or interface change; new session snapshot required')
        return outcome
    def event(operation,payload):
        nonlocal sequence
        recheck()
        sequence+=1
        emit(dict(schema=1,nonce=nonce,producer=producer,sequence=sequence,operation=operation,
                  emitted_monotonic=clock(),payload=payload))
    def pulse():
        # A heartbeat from inside a long stretch of work, so a burst of events or a
        # refresh never leaves the relay silent for its silence budget.
        nonlocal heartbeat
        if heartbeat is not None and clock()-heartbeat>=1:event('heartbeat',{});heartbeat=clock()
    def check_context():
        if feed.runtime.saved()!=state or feed.acknowledgement(feed.runtime.status(),state)!=collector:
            raise ValueError('Applied policy, bindings or Security collector changed')
    def plan(value):
        return withheld(feed.l3.plan(value,state['bindings'],topology),changed)
    # Subscribe before dumping, then replay interleaved changes. Never label a
    # truncated dump or a lost receive queue as a synchronized snapshot. The
    # generation's first message leaves before the conntrack dump: the relay's
    # silence budget starts at its ssh hops, and the context, topology and dump
    # together ran past it at 2,300 sessions.
    with route_watch() as routes,subscribe() as source:
        deadline=clock()+SNAPSHOT_BUDGET
        topology=feed.l3.snapshot(deadline)
        event('begin',policy_context(state,collector,topology,table_limits()))
        rows,changes=snapshot(source,timeout=3,capacity=CAPACITY)
        rows=replay(rows,changes);check_context();recheck()
        def assess(row):
            value=feed.assess(row,rules,producer['boot_id'])
            value['l3']=plan(value)
            return value
        chunk=[]
        for row in rows:
            if clock()>=deadline:raise EventGap('Session/L3 snapshot exceeded freshness budget')
            assessed=assess(row);known[assessed['identity']]=assessed['token'];emitted[assessed['identity']]=assessed;chunk.append(assessed)
            if len(chunk)==CHUNK:event('snapshot',chunk);chunk=[]
        if chunk:event('snapshot',chunk)
        if clock()>=deadline:raise EventGap('Session/L3 snapshot exceeded freshness budget')
        check_context();recheck();event('synchronized',{})
        def flush(changes):
            for batch in batches(changes):event('changes',batch)
            changes.clear()
        def handle(batch,changes):
            for row in batch:
                pulse()
                # Closed/unowned sessions need only their identity; do
                # not resolve next hops on the withdrawal path.
                assessed=feed.assess(row,rules,producer['boot_id']);ident=assessed['identity']
                if row['event']!='end' and row['token'] is None and ident in known:
                    row=with_known_owner(row,ident,known);assessed=feed.assess(row,rules,producer['boot_id'])
                if row['event']=='end' or row['token'] is None:
                    if ident in known:changes.append({'op':'close','identity':ident});known.pop(ident);emitted.pop(ident,None)
                else:
                    assessed['l3']=plan(assessed)
                    # An event without counters is silence about traffic, not a reset: the counts
                    # last emitted stand until the next event or refresh moves them.
                    if not counters_known(row) and (emitted.get(ident) or {}).get('counters'):
                        assessed['counters']=emitted[ident]['counters']
                    known[ident]=assessed['token'];emitted[ident]=assessed
                    if len(known)>CAPACITY:raise EventGap('Session stream capacity exceeded')
                    changes.append({'op':'upsert','row':assessed})
                if len(changes)>=BATCH:flush(changes)
        heartbeat=refresh=clock()
        while not stop():
            now=clock()
            if now-heartbeat>=1:
                check_context();event('heartbeat',{});heartbeat=clock()
            if now-refresh>=REFRESH_SECONDS:
                # The counters of the candidate rows, from a dump on the same subscribed socket;
                # events that arrive during the dump are ordinary events and follow it in order.
                check_context()
                dumped,pending=snapshot(source,timeout=3,capacity=CAPACITY)
                recheck()
                updates=refreshed(dumped,emitted,producer['boot_id'])
                for row in updates:emitted[row['identity']]=row
                for start in range(0,len(updates),BATCH):
                    pulse();event('changes',[{'op':'upsert','row':row} for row in updates[start:start+BATCH]])
                if pending:
                    changes=[];handle(pending,changes);flush(changes)
                refresh=clock()
            readable=select.select([source,routes],[],[],.1)[0]
            if routes in readable:recheck()
            if source not in readable:continue
            # Bound each burst so traffic cannot starve context checks. The burst
            # leaves as one ordered `changes` batch: the relay forwards it to the
            # CP in a single round trip, which is what keeps receipt within the
            # backlog budget under hundreds of conntrack events per second.
            deadline=clock()+.05;count=0;changes=[]
            while count<2048 and clock()<deadline:
                try:batch=receive(source)
                except BlockingIOError:break
                count+=len(batch)
                handle(batch,changes)
            flush(changes)


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
                # telemetry, not permission to reuse the previous snapshot. The
                # pause stays short: the relay's silence budget runs from this
                # message to the next generation's first, and the context and
                # topology reads already take a few seconds on the box.
                reason=str(error)[:512] or 'DP context unavailable'
                print('generation ended: '+reason,file=sys.stderr,flush=True)
                send(1,dict(schema=1,nonce=request['nonce'],unavailable=reason))
                time.sleep(1)
    except (Exception,KeyboardInterrupt) as error:
        print(str(error)[:512],file=sys.stderr);raise SystemExit(2)
