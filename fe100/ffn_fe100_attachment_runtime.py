"""Resolve current MP intent against CP ownership journals and fresh DP bindings.

Read-only prerequisite resolution: no table allocation or ingress redirection.
Journal observations are not fresh hardware readback or forwarding qualification.
"""
import copy
import json
from pathlib import Path
import time
from fe100_attachment_config import checksum,validate


def journals():
    from ffn_copper_forwarding import epoch
    root=Path('/etc/ffn');result={'epoch':epoch(),'physical':{},'groups':{}}
    for port in range(1,25):
        path=root/('wan-forwarding.json' if port==1 else 'physical-'+str(port)+'.json')
        if path.exists():result['physical'][str(port)]=json.loads(path.read_bytes())
    path=root/'aggregate-hardware.json'
    if path.exists():result['groups']=json.loads(path.read_bytes()).get('groups',{})
    if epoch()!=result['epoch']:raise RuntimeError('BCM owner changed during attachment observation')
    return result


def resolve(configuration,receiver,config_digest,read=journals,clock=time.monotonic):
    unavailable=lambda reason:dict(available=False,hardware_admission=False,reason=reason,interfaces=[])
    if configuration is None:return unavailable('Awaiting committed interface intent from MP')
    validate(configuration)
    if configuration['config_digest']!=config_digest:return unavailable('Committed interface intent differs from policy barrier')
    if receiver is None or not receiver.ready:return unavailable('Awaiting a fresh acknowledged DP snapshot')
    receiver.tick()
    hardware=read();now=clock();rows=[]
    for intent in configuration['interfaces']:
        blockers=[];binding=receiver.policy['bindings'].get(intent['name']);identity=None
        if not intent['enabled']:blockers.append('Interface or configured member is administratively disabled')
        if intent['zone'] is None or intent['vsys'] is None:blockers.append('Layer 3 zone ownership is not assigned')
        if not isinstance(binding,dict):blockers.append('Interface is absent from acknowledged DP policy bindings')
        if intent['kind']=='physical':
            state=hardware['physical'].get(str(intent['ports'][0]),{})
            if (state.get('epoch')!=hardware['epoch'] or state.get('dp_boot_id')!=receiver.producer['boot_id'] or
                    state.get('enabled') is not True or state.get('pending') is not None):
                blockers.append('CP physical redirect ownership is missing, stale or pending')
            else:identity={k:state.get(k) for k in ('epoch','dp_boot_id','enabled','revision','port')}
            if isinstance(binding,dict) and (intent['vlan'] or binding.get('device')!='p'+str(intent['ports'][0])):
                # Physical subinterface packet attachment is not commissioned
                # by the current physical runtime, so never infer it from a name.
                blockers.append('Physical DP device attachment is not commissioned')
        else:
            state=hardware['groups'].get(intent['parent'],{})
            stamp=state.get('heartbeat')
            if (state.get('epoch')!=hardware['epoch'] or state.get('phase')!='active' or
                    type(stamp) not in (int,float) or not 0<=now-stamp<15 or
                    sorted(state.get('ports',[]))!=intent['ports'] or state.get('pending_egress') is not None):
                blockers.append('CP aggregate member ownership is missing, stale or pending')
            else:identity={k:state.get(k) for k in ('epoch','token','ports','offload','trunk_created','egress')}
            if isinstance(binding,dict) and binding.get('alias')!='ffn-aggregate:'+str(state.get('token'))+':'+intent['name']:
                blockers.append('DP aggregate owner differs from CP member owner')
            if state.get('offload') is not True or state.get('trunk_created') is not True or state.get('egress')!=intent['ports']:
                blockers.append('Aggregate hardware egress selection is not commissioned')
        observed=not blockers
        revision=checksum(dict(config=configuration['config_digest'],intent=intent,binding=binding,
            owner=identity,producer=receiver.producer)) if observed else None
        rows.append(dict(intent=copy.deepcopy(intent),binding=copy.deepcopy(binding),
            ownership_observed=observed,binding_revision=revision,blockers=blockers,
            remaining=['Commissioned FE100 zone and miss path','LIF/LEF and QMAP allocation',
                       'BCM FE100 ingress and egress readback','Counter and exception handoff','Trusted session admission']))
    receiver.tick()
    # Status uses the 64-KiB root RPC envelope. Keep the complete intent in the
    # owner, but bound the diagnostic projection rather than truncating JSON.
    visible=[];size=0
    for row in rows:
        size+=len(json.dumps(row,separators=(',',':')).encode())
        if size>24576:break
        visible.append(row)
    return dict(available=True,hardware_admission=False,config_digest=configuration['config_digest'],
        intent_digest=checksum(configuration),interfaces=visible,reason=None,total=len(rows),
        ownership_observed=sum(r['ownership_observed'] for r in rows),truncated=len(visible)!=len(rows))
