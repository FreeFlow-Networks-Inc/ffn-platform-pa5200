"""Supervised dry-run evaluation of FE100 hardware admission.

Internal control component. Over the session rows the supervised MP relay
holds, it runs the gate chain production admission applies (session
candidacy, policy generation, interface attachment ownership, L3 next-hop
planning, paired request encoding and directional path plan shape) and
reports, per session, what still blocks hardware admission. It writes no
table, allocates no resource, takes no lease and changes no policy; hardware
admission stays disabled. A session with no per-session reason is admissible
only once the generation and commissioning items listed with it are closed.
"""
import collections
import json
from ffn_fe100_path_owner import PathOwner
from ffn_fe100_policy import digest
from ffn_fe100_nat import session_pair4

MODE='supervised-dry-run'
MAX_EVALUATED=128      # bounded work per status call on the CP
PROJECTION_BYTES=16384 # share of the 64 KiB root RPC envelope
TOP_REASONS=16
STAGES=('software','blocked','admissible-pending-commissioning')
PLACEHOLDER='0'*64
UNCOMMISSIONED=['FE100 ingress zone and miss path per attachment','egress LIF per attachment',
                'durable flow-ID namespace','directional next-hop and source-MAC leases']


def rule_id(rule,token):
    """Rule identity in the admission request: scope/name of the granting rule."""
    scope,name=(rule or {}).get('scope'),(rule or {}).get('name')
    value=scope+'/'+name if isinstance(scope,str) and isinstance(name,str) else token
    return value if isinstance(value,str) and 1<=len(value)<=128 else None


def generation(owner,flow_ids,qualified,configuration_digest):
    """Reasons that block every session of the held generation.

    The DP's applied policy generation (its own revision and digest) is fenced
    by the observation intake whenever it changes; the barrier the owner holds
    is the committed configuration digest the MP relays with the interface
    intent, so that is what must agree with the owner.
    """
    reasons=[]
    if owner.get('admission_enabled') is not True:
        reasons.append('policy generation is not activated for hardware admission (phase %s)'%owner.get('phase'))
    if qualified is not True:reasons.append('front-port offload is not qualified')
    if flow_ids is not True:reasons.append('durable hardware flow-ID allocator is not commissioned')
    if configuration_digest is None:reasons.append('committed interface intent has not been relayed for this generation')
    elif configuration_digest!=owner.get('digest'):
        reasons.append('relayed configuration intent differs from the CP policy barrier')
    return reasons


def request_for(row,policy,owner,indices,names):
    """The paired admission request the production path would receive.

    Revision and policy digest are the CP owner's barrier (what admit_pair
    compares against its state); the NAT digest is the DP's NAT generation.
    """
    rule=row.get('rule') or {};nat=row.get('nat') or {};proto=(row.get('original') or {}).get('protocol')
    return dict(session_id=row.get('conntrack_id'),revision=owner.get('revision'),
                policy_digest=owner.get('digest'),nat_digest=policy.get('nat_digest'),
                rule_id=rule_id(rule,row.get('token')),verdict='allow',
                original=row.get('original'),reply=row.get('reply'),
                ingress=indices.get(names[0]) if names else None,egress=indices.get(names[1]) if names else None,
                inspection_required=rule.get('inspection_required'),
                nat_required=bool(nat.get('source') or nat.get('destination')),
                established=proto==17 or (proto==6 and row.get('tcp_state')==3))


def plan_for(row,policy,l3,indices,names,attachments):
    """The path-owner plan shape, with the uncommissioned zone/LIF as zero."""
    expected=[(names[0],names[1],(row.get('translated') or {}).get('destination')),
              (names[1],names[0],(row.get('original') or {}).get('source'))]
    directions=l3.get('directions')
    if not isinstance(directions,list) or len(directions)!=2:raise ValueError('two directional plans required')
    plan=[]
    for d,(ingress,egress,destination) in zip(directions,expected):
        if not isinstance(d,dict) or d.get('interface')!=egress or d.get('destination')!=destination:
            raise ValueError('directional plan does not match the translated destination')
        if d.get('index')!=indices[egress]:raise ValueError('directional plan owner differs from applied binding: '+egress)
        attachment=attachments.get(egress)
        revision=attachment['binding_revision'] if attachment and attachment.get('ownership_observed') else PLACEHOLDER
        plan.append(dict(ingress=indices[ingress],egress=indices[egress],destination=destination,zone=0,egress_lif=0,
                         source_mac=d.get('source_mac'),destination_mac=d.get('destination_mac'),
                         vlan=d.get('vlan'),mtu=d.get('mtu'),route_revision=l3.get('snapshot_digest'),
                         neighbor_revision=digest([d.get('next_hop'),d.get('destination_mac')]),
                         attachment_revision=revision))
    PathOwner.validate_plan(dict(nat_digest=policy.get('nat_digest'),directions=plan))
    return plan


def evaluate_row(row,policy,owner,attachments,truncated,nat_qualified):
    item=dict(identity=row.get('identity'),session_id=row.get('conntrack_id'),interfaces=[],
              protocol=(row.get('original') or {}).get('protocol'),nat=None,established=None)
    if row.get('software_candidate') is not True:
        return dict(item,stage='software',blockers=list(row.get('blockers') or ['not a software candidate']))
    reasons=[];rule=row.get('rule') or {};pairs=rule.get('interface_pairs') or []
    l3=row.get('l3') if isinstance(row.get('l3'),dict) else {}
    # The DP's L3 plan names the pair the routes selected among those the rule
    # authorises; a legacy plan only exists for rules with a single pair.
    selected=l3.get('pair')
    if isinstance(selected,list) and len(selected)==2 and list(selected) in [list(p) for p in pairs if isinstance(p,list)]:
        names=tuple(selected)
    elif len(pairs)==1 and len(pairs[0])==2:names=tuple(pairs[0])
    else:names=None
    if names is None:reasons.append('interface pair unresolved by the routes among %d authorised' % len(pairs))
    bindings=policy.get('bindings') or {};indices={}
    for name in names or ():
        binding=bindings.get(name)
        if not isinstance(binding,dict) or type(binding.get('index')) is not int:
            reasons.append('interface owner is absent from applied bindings: '+name)
        else:indices[name]=binding['index']
        attachment=attachments.get(name)
        if attachment is None:
            reasons.append(('attachment intent projection is truncated: ' if truncated else
                            'no committed Layer 3 attachment intent: ')+name)
        elif attachment.get('ownership_observed') is not True:
            reasons.extend(name+': '+str(r) for r in attachment.get('blockers') or ['attachment ownership is not observed'])
    item['interfaces']=list(names or ())
    request=request_for(row,policy,owner,indices,names)
    item.update(nat=request['nat_required'],established=request['established'])
    if request['rule_id'] is None:reasons.append('granting rule identity is unavailable')
    if request['inspection_required'] is not False:reasons.append('flow requires software enforcement: inspection profile')
    if request['established'] is not True:reasons.append('flow requires software enforcement: not established')
    if request['nat_required'] and nat_qualified is not True:
        reasons.append('NAT packet rewrite is not qualified for hardware admission')
    if len(indices)==2 and request['ingress']==request['egress']:reasons.append('verified distinct front-port bindings required')
    try:session_pair4(request['session_id'],request['original'],request['reply'],0,[0,0])
    except (ValueError,TypeError,KeyError) as error:reasons.append('session tuple encoding: '+str(error))
    l3=row.get('l3')
    if not isinstance(l3,dict):reasons.append('DP route/neighbor observation is unavailable')
    elif l3.get('available') is not True:
        reasons.extend(str(r) for r in l3.get('blockers') or ['DP route/neighbor planning is unavailable'])
    elif names and len(indices)==2:
        try:plan_for(row,policy,l3,indices,names,attachments)
        except (ValueError,TypeError,KeyError) as error:reasons.append('directional path plan: '+str(error))
    return dict(item,stage='blocked' if reasons else 'admissible-pending-commissioning',blockers=reasons)


def evaluate(receiver,attachments,owner,capabilities,flow_ids=False,qualified=False,
             configuration_digest=None,limit=PROJECTION_BYTES,bound=MAX_EVALUATED):
    """Dry-run the admission gate chain over the supervised inventory."""
    base=dict(mode=MODE,hardware_admission=False,installed=0)
    if receiver is None or getattr(receiver,'ready',False) is not True or not isinstance(receiver.policy,dict):
        return dict(base,available=False,reason='supervised observation inventory is not ready',evaluated=0,total=0)
    policy=receiver.policy;owner=owner if isinstance(owner,dict) else {}
    truncated_intent=False;intents={}
    if isinstance(attachments,dict) and attachments.get('available') is True:
        intents={r['intent']['name']:r for r in attachments.get('interfaces') or ()}
        truncated_intent=attachments.get('truncated') is True
    nat_qualified=isinstance(capabilities,dict) and capabilities.get('nat_packet_qualification') is True
    common=generation(owner,flow_ids,qualified,configuration_digest)
    rows=[];reasons=collections.Counter();remaining=set()
    inventory=sorted(receiver.sessions.items())[:bound]
    for identity,row in inventory:
        item=evaluate_row(row,policy,owner,intents,truncated_intent,nat_qualified)
        for name in item['interfaces']:
            remaining.update(str(r) for r in (intents.get(name) or {}).get('remaining') or ())
        reasons.update(item['blockers']);rows.append(item)
    visible=[];size=0
    for item in rows:
        size+=len(json.dumps(item,separators=(',',':')).encode())
        if size>limit:break
        visible.append(item)
    stages={stage:sum(r['stage']==stage for r in rows) for stage in STAGES}
    return dict(base,available=True,evaluated=len(rows),total=len(receiver.sessions),
                bounded=len(rows)!=len(receiver.sessions),stages=stages,
                admissible_pending_commissioning=stages['admissible-pending-commissioning'],
                generation=common,commissioning=sorted(remaining)+UNCOMMISSIONED,
                reasons=dict(reasons.most_common(TOP_REASONS)),sessions=visible,truncated=len(visible)!=len(rows))
