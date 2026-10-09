#!/usr/bin/env python3
"""CP observation-only FE100 planning. Never allocate IDs or write tables.

MP obtains this envelope through pinned DP SSH, with a fresh nonce and bounded
round trip. This interface is not an admission API or an ordered event feed.
"""
import ipaddress
import json
import math
import re
import sys
import uuid


def integer(value,low,high):
    return type(value) is int and low<=value<=high


def tuple4(value):
    if not isinstance(value,dict) or set(value)!={'source','destination','source_port','destination_port','protocol'}:
        raise ValueError('Complete IPv4 transport tuple required')
    if type(value['protocol']) is not int or value['protocol'] not in (6,17):raise ValueError('Unsupported protocol')
    for field in ('source','destination'):
        if not isinstance(value[field],str) or str(ipaddress.IPv4Address(value[field]))!=value[field]:
            raise ValueError('Canonical IPv4 address required')
    for field in ('source_port','destination_port'):
        if not integer(value[field],0,65535):raise ValueError('Invalid transport port')
    return dict(value)


def reverse(value):
    return dict(source=value['destination'],destination=value['source'],source_port=value['destination_port'],
                destination_port=value['source_port'],protocol=value['protocol'])


def l3_observation(value, pairs, owners, destinations):
    """Validate a DP next-hop observation; never promote it to admission."""
    if value is None:
        return dict(available=False,hardware_admission=False,directions=[],
                    blockers=['DP route/neighbor observation is unavailable'])
    if (not isinstance(value,dict) or type(value.get('available')) is not bool or
        value.get('hardware_admission') is not False or not isinstance(value.get('blockers'),list) or
        any(not isinstance(reason,str) for reason in value['blockers'])):
        raise ValueError('Invalid DP L3 observation')
    if not value['available']:
        if not value['blockers'] or value.get('directions')!=[]:raise ValueError('Invalid blocked L3 observation')
        return value
    if (value['blockers'] or not re.fullmatch('[0-9a-f]{64}',value.get('snapshot_digest','')) or
        not isinstance(value.get('directions'),list) or len(value['directions'])!=2):
        raise ValueError('Incomplete DP L3 observation')
    for direction,name,destination in zip(value['directions'],reversed(pairs),destinations):
        binding=owners.get(name,{})
        if (direction.get('interface')!=name or direction.get('destination')!=destination or
            any(direction.get(k)!=binding.get(k) for k in ('device','index','alias')) or
            direction.get('decrement_ttl') is not True or
            direction.get('exceptions')!=['ttl-expired','mtu-exceeded','ipv4-fragments'] or
            not integer(direction.get('mtu'),576,9216) or
            direction.get('vlan') is not None and not integer(direction['vlan'],1,4094)):
            raise ValueError('L3 route does not match the acknowledged session binding')
        ipaddress.IPv4Address(direction['next_hop'])
        for key in ('source_mac','destination_mac'):
            mac=direction.get(key)
            if not isinstance(mac,str) or not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}',mac):
                raise ValueError('Invalid next-hop Ethernet address')
            raw=bytes.fromhex(mac.replace(':',''))
            if not any(raw) or raw[0]&1:raise ValueError('Non-unicast next-hop Ethernet address')
    return value


def plan(request,hardware):
    if not isinstance(request,dict) or set(request)!={'nonce','observation'}:raise ValueError('Invalid plan envelope')
    nonce=request['nonce'];data=request['observation']
    if not isinstance(nonce,str) or str(uuid.UUID(nonce))!=nonce:raise ValueError('Canonical nonce required')
    if (not isinstance(data,dict) or data.get('schema')!=1 or data.get('nonce')!=nonce or
        data.get('available') is not True or data.get('hardware_admission') is not False or
        data.get('source')!='kernel-conntrack-with-durable-security-grants'):
        raise ValueError('Current acknowledged DP observation required')
    start,end=data['observed_monotonic'],data['completed_monotonic']
    if any(type(v) not in (int,float) or not math.isfinite(v) or v<0 for v in (start,end)) or not 0<=end-start<=8:
        raise ValueError('Observation exceeded freshness budget')
    producer=data['producer'];policy=data['policy']
    if (str(uuid.UUID(producer['boot_id']))!=producer['boot_id'] or not integer(producer['pid'],1,2**31-1) or
        not isinstance(producer['process_start'],str) or not producer['process_start'].isdigit() or
        not integer(producer['reconciliations'],0,2**63-1)):
        raise ValueError('Invalid session producer identity')
    if not integer(policy['revision'],0,2**63-1):raise ValueError('Invalid applied generation')
    for field in ('digest','nat_digest'):
        if not isinstance(policy[field],str) or not re.fullmatch('[0-9a-f]{64}',policy[field]):raise ValueError('Invalid policy digest')
    if not isinstance(policy['bindings'],dict):raise ValueError('Interface owner bindings required')
    sessions=data['sessions']
    if (not isinstance(sessions,list) or len(sessions)>128 or not integer(data['owned_sessions'],len(sessions),8192) or
        type(data['truncated']) is not bool or data['truncated']!=(data['owned_sessions']>len(sessions))):
        raise ValueError('Invalid bounded session inventory')
    pending=['ordered session lifecycle feed is not connected','FE100 zone and directional next hops are not commissioned',
             'hardware aging and Security logging/counter handoff are not qualified']
    hardware_blockers=list(hardware.get('blockers',[]))+list(hardware.get('action_blockers',[]))
    rows=[];identities=set()
    for row in sessions:
        identity=row['identity']
        if not isinstance(identity,str) or not re.fullmatch('[0-9a-f]{64}',identity) or identity in identities:
            raise ValueError('Duplicate or invalid session identity')
        identities.add(identity)
        reasons=list(row['blockers'])
        if type(row['software_candidate']) is not bool or row['software_candidate']!= (not reasons):
            raise ValueError('Inconsistent software eligibility')
        item=dict(identity=identity,conntrack_id=row['conntrack_id'],rule=row['rule'],
                  software_candidate=row['software_candidate'],hardware_eligible=False,
                  remaining_seconds=row['remaining_seconds'],counters=row['counters'])
        if row['software_candidate']:
            original,reply=tuple4(row['original']),tuple4(row['reply'])
            if original['protocol']!=reply['protocol']:raise ValueError('Directional protocol mismatch')
            translated=reverse(reply)
            if row['translated']!=translated:raise ValueError('Translation does not match kernel reply tuple')
            pairs=row['rule']['interface_pairs']
            if not pairs or any(not isinstance(p,list) or len(p)!=2 for p in pairs):raise ValueError('Ambiguous interface pair')
            # The DP's L3 plan names the pair the routes selected among those
            # the rule authorises; without it only a single authorised pair is known.
            observed=(row.get('l3') or {}).get('pair')
            if observed in pairs:pair=observed
            elif len(pairs)==1:pair=pairs[0]
            else:
                reasons.append('interface pair unresolved: the rule authorises %d pairs and the routes selected none of them' % len(pairs));pair=pairs[0]
            owners={}
            for name in pair:
                binding=policy['bindings'].get(name)
                if (not isinstance(binding,dict) or not integer(binding.get('index'),1,2**31-1) or
                    not isinstance(binding.get('device'),str) or not isinstance(binding.get('alias'),str)):
                    reasons.append('interface owner is absent from applied bindings: '+name)
                else:owners[name]=binding
            item['interfaces']=owners
            item['directions']=[dict(match=original,translated=translated),dict(match=reply,translated=reverse(original))]
            item['nat']=any(original[k]!=translated[k] for k in original)
            if item['nat']:reasons.append('FE100 NAT packet/checksum qualification is incomplete')
            item['interface_pair']=list(pair)
            item['l3']=l3_observation(row.get('l3'),pair,owners,
                                      [translated['destination'],original['source']])
            reasons.extend(item['l3']['blockers'])
            reasons.append('ordered route/neighbor invalidation and hardware exception handling are not connected')
        item['blockers']=reasons+hardware_blockers+pending
        rows.append(item)
    return dict(schema=1,nonce=nonce,available=True,mode='observation-only',producer=producer,policy=policy,
                hardware_admission=False,hardware_initialized=hardware.get('initialized') is True,
                hardware_blockers=hardware_blockers,owned_sessions=data['owned_sessions'],truncated=data['truncated'],
                software_candidates=sum(row['software_candidate'] for row in rows),sessions=rows,
                note='Observed sessions remain enforced by the dataplane kernel; no FE100 flow was installed.')


if __name__=='__main__':
    try:
        raw=sys.stdin.buffer.read(524289)
        if len(raw)>524288:raise ValueError('Session plan exceeds 512 KiB')
        request=json.loads(raw)
        from ffn_fe100_live_sessions import LiveSessions
        result=plan(request,LiveSessions().status())
        # The supervised owner's dry-run admission evaluation over the live
        # relay inventory; unavailable when the control service is not serving.
        try:
            from ffn_fe100_policy_control import dispatch
            result['supervised']=dispatch('status',{}).get('admission') or dict(available=False,reason='no admission evaluation')
        except Exception as error:
            result['supervised']=dict(mode='supervised-dry-run',hardware_admission=False,available=False,reason=str(error)[:256])
        print(json.dumps(result))
    except (ValueError,KeyError,TypeError,OSError,RuntimeError) as error:
        print(json.dumps({'error':str(error)[:512]}));raise SystemExit(2)
