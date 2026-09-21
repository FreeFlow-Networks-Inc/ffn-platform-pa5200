#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""MP lifecycle owner: committed XML -> CP redirect lease -> DP LACP/TAP owner."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import time
import uuid
from xml.etree import ElementTree as ET
sys.path.extend(['/opt/ffn-ngfw','/opt/ffn-ngfw-v2'])
from aggregate_config import plan,parse

DIRECTORY=Path('/var/lib/ffn-ngfw/aggregate-runtime')
RUNNING=Path('/var/lib/ffn-ngfw/config/running-config.xml')
OFFLOAD_BLOCKER='BCM egress is implemented but TM hash distribution is not commissioned; use OCTEON software activation'
CP=['ssh','-F','/etc/ffn-ngfw/ssh-cp.conf','-o','BatchMode=yes','ffn-cp']
DP=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=5','-o','ServerAliveInterval=3','-o','ServerAliveCountMax=2',
    '-o','UserKnownHostsFile=/etc/ffn-ngfw/plane_boot_known_hosts',
    '-o','ProxyCommand=ssh -F /etc/ffn-ngfw/ssh-cp.conf -W %h:%p ffn-cp','root@127.1.2.2']


def atomic(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    with temp.open('w') as out:json.dump(data,out);out.flush();os.fsync(out.fileno())
    temp.chmod(0o600);temp.replace(path)


def command(role,operation):
    if role=='cp' and operation in ('status','prepare','stop','recover','stream'):
        return CP+['python3 /usr/local/sbin/ffn_aggregate_hardware.py '+operation]
    if role=='dp' and operation in ('status','serve','recover'):
        return DP+['python3 /usr/local/sbin/ffn_aggregate_runtime.py '+operation]
    raise ValueError('Unknown aggregate agent operation')


def remote(role,operation,payload):
    result=subprocess.run(command(role,operation),input=json.dumps(payload)+'\n',text=True,capture_output=True,timeout=45)
    if result.returncode:raise RuntimeError(role+' aggregate operation failed: '+result.stdout[-1024:])
    value=json.loads(result.stdout)
    if not isinstance(value,dict) or value.get('error'):raise RuntimeError('Invalid aggregate agent response')
    return value


def valid_name(name):
    if not isinstance(name,str) or not re.fullmatch(r'ae(?:[1-9]|1[0-2])',name):raise ValueError('Invalid aggregate name')
    return name


def status():
    groups={}
    for path in DIRECTORY.glob('ae*-status.json'):
        try:
            row=json.loads(path.read_text());valid_name(row['group'])
            start=Path('/proc',str(row['pid']),'stat').read_text().rsplit(') ',1)[1].split()[19]
            fresh=row.get('state') not in ('starting','stopping','stopped','failed') and start==row['process_start'] and row['boot_id']==Path('/proc/sys/kernel/random/boot_id').read_text().strip() and 0<=time.monotonic()-row['updated_monotonic']<6 and 0<=time.monotonic()-row.get('dp_received_monotonic',0)<3
        except (OSError,ValueError,KeyError,IndexError):
            try:row=json.loads(path.read_text());fresh=False
            except (OSError,ValueError):continue
        row['fresh']=fresh
        if not fresh:
            row['state']='stopped' if row.get('state')=='stopped' else 'unavailable'
            row['applied']=False
            if isinstance(row.get('dataplane'),dict):
                row['dataplane'].update(distributing=[],attachment_ready=False,network_ready=False,hardware_offload=False,configuration_revision=None)
                for unit in row['dataplane'].get('subinterfaces',[]):unit.update(applied=False,state='pending')
        groups[row['group']]=row
    return dict(groups=groups,revision=revision(),activation_supported=Path('/etc/systemd/system/ffn-aggregate@.service').is_file(),
                offload_ready=False,offload_blocker=OFFLOAD_BLOCKER,
                backend='octeon-aggregate',hardware_offload=any(r.get('fresh') and r.get('dataplane',{}).get('hardware_offload') for r in groups.values()))


def revision():
    path=DIRECTORY/'revision.json'
    return json.loads(path.read_text())['revision'] if path.exists() else 0


def parent_revision(raw,group):
    """Fingerprint all config except this aggregate's unimplemented VLAN units.

    Active network, member, profile and policy edits must still withdraw the
    owner. Adding/editing a VLAN unit must not stop parent LACP negotiation.
    """
    valid_name(group)
    root=parse(raw)
    path="./devices/entry[@name='localhost.localdomain']/network/interface/aggregate-ethernet/entry"
    for entry in root.findall(path):
        if entry.get('name')!=group:continue
        l3=entry.find('layer3')
        if l3 is not None:
            for units in l3.findall('units'):l3.remove(units)
    device=root.find("./devices/entry[@name='localhost.localdomain']")
    if device is not None:
        # The candidate editor also imports units and assigns existing zones/VRs.
        # Ignore only these unit memberships, never parent or policy references.
        containers=[]
        def last_child(parent,tag):
            node=parent.find(tag)
            if node is None:node=ET.Element(tag)
            else:parent.remove(node)
            parent.append(node)
            return node
        for owner in device.findall('vsys/entry'):
            node=owner
            for tag in ('import','network','interface'):
                node=last_child(node,tag)
            containers.append(node)
            containers.extend(owner.findall('zone/entry/network/layer3'))
        for router in device.findall('network/virtual-router/entry'):
            containers.append(last_child(router,'interface'))
        for container in containers:
            for member in list(container):
                if member.tag=='member' and re.fullmatch(re.escape(group)+r'\.[0-9]+',member.text or ''):
                    container.remove(member)
    canonical=ET.canonicalize(ET.tostring(root,encoding='unicode'),strip_text=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def prepare(raw,group,control_only,dp_boot,offload=False):
    compiled=plan(raw);rows=[g for g in compiled['aggregates'] if g['ae_name']==group]
    if len(rows)!=1:raise ValueError('One committed aggregate definition required')
    row=rows[0]
    if row['errors']:raise ValueError('; '.join(row['errors']))
    if not row['enabled'] or any(not p['enabled'] for p in row['members']):raise ValueError('Enable the aggregate and members in committed configuration first')
    if any(p['port']<=4 for p in row['members']):raise ValueError('This aggregate owner currently supports optical members only')
    if row['network']['mtu']>1500:raise ValueError('Aggregate packet path supports MTU up to 1500')
    root=parse(raw);device=root.find("./devices/entry[@name='localhost.localdomain']")
    from ffn_interface_management import profile
    network=dict(row['network']);network['management']=profile(device,network.pop('management_profile'))
    network['units']=[{k:u[k] for k in ('name','tag','addresses','mtu','management')} for u in row['subinterfaces'] if u['supported']]
    system='02:'+':'.join(hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest()[n:n+2] for n in range(0,10,2))
    intent=dict(group=group,token=str(uuid.uuid4()),boot_id=dp_boot,system=system,members=[p['port'] for p in row['members']],
        lacp=row['lacp'],network=network,lldp=row['lldp'],control_only=control_only,offload=offload)
    return dict(group=group,activation_id=intent['token'],running_revision=compiled['revision'],parent_revision=parent_revision(raw,group),link_revision=link_revision(row),intent=intent,
        speeds={str(p['port']):p['speed'] for p in row['members']})


def link_revision(row):
    value={key:row[key] for key in ('ae_name','members','bonding_mode','enabled','lacp')}
    value['members']=sorted(value['members'],key=lambda member:member['port'])
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def network_revision(intent):
    return hashlib.sha256(json.dumps({key:intent[key] for key in ('network','lldp')},sort_keys=True).encode()).hexdigest()


def resume_selection(name,call=remote):
    """Fence the previous owner, then compile a new lifetime from running XML.

    Called only by a started supervisor. Explicit Stop remains stopped. A new
    token and an empty observation ensure an old configuration ACK cannot be
    reused even when the DP process restarts within the same boot.
    """
    import fcntl
    with (DIRECTORY/'lifecycle.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        path=DIRECTORY/(valid_name(name)+'-intent.json')
        saved=json.loads(path.read_text());old=saved['intent'];raw=RUNNING.read_bytes()
        dp=call('dp','status',{});cp=call('cp','status',{})
        selected=prepare(raw,name,old['control_only'],dp['boot_id'],old['offload'])
        if old['offload']:raise ValueError(OFFLOAD_BLOCKER)
        observed=dp.get('groups',{}).get(name,{})
        if observed.get('fresh'):raise RuntimeError('Previous DP owner is still active; awaiting withdrawal')
        if observed and observed.get('token')!=old['token']:raise ValueError('DP aggregate ownership changed')
        for group,state in cp['groups'].items():
            if state['phase']=='stopped':continue
            if group!=name and set(state['ports'])&set(selected['intent']['members']):raise ValueError('Aggregate members belong to another CP owner')
            if group!=name:continue
            if state['token']!=old['token'] or state['epoch']!=saved['epoch']:raise ValueError('CP aggregate ownership changed')
            if state['epoch']==cp['epoch']:
                call('cp','stop',dict(group=name,token=old['token'],epoch=cp['epoch']))
            else:
                call('cp','recover',dict(group=name,token=old['token'],epoch=cp['epoch'],previous_epoch=state['epoch']))
        # /run is cleared on a reboot. Never send the previous boot's cleanup
        # identity into the new boot or remove a new owner's interfaces.
        if dp['boot_id']==old['boot_id']:
            call('dp','recover',{key:old[key] for key in ('group','token','boot_id')})
        if RUNNING.read_bytes()!=raw:raise ValueError('Running configuration changed during recovery')
        from policy_guard import before_commit
        before_commit(raw)
        selected['epoch']=cp['epoch']
        selected['activation_id']=saved.get('activation_id',old['token'])
        atomic(path,selected)
        return selected


def execute(action,payload,call=remote):
    if action=='status':
        if payload:raise ValueError('Status takes no fields')
        return status()
    if action not in ('validate','apply') or set(payload)!={'group','operation','running_revision','revision'}:raise ValueError('Expected group, operation, running revision and controller revision')
    name=valid_name(payload['group']);operation=payload['operation']
    if operation not in ('activate','offload','negotiate','deactivate','recover'):raise ValueError('Unknown aggregate lifecycle operation')
    if operation=='offload':raise ValueError(OFFLOAD_BLOCKER)
    import fcntl
    raw=RUNNING.read_bytes()
    if payload['running_revision']!=hashlib.sha256(raw).hexdigest():raise ValueError('Running configuration changed; refresh')
    DIRECTORY.mkdir(parents=True,exist_ok=True)
    with (DIRECTORY/'lifecycle.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if type(payload['revision']) is not int or payload['revision']!=revision():raise ValueError('Aggregate revision conflict; refresh')
        if operation in ('deactivate','recover'):
            if action=='validate':return {'validated':True}
            atomic(DIRECTORY/'revision.json',{'revision':revision()+1})
            subprocess.run(['systemctl','stop','ffn-aggregate@'+name],check=True,timeout=60)
            current=call('cp','status',{})
            state=current['groups'].get(name)
            if state and state['phase']!='stopped':call('cp','stop',dict(group=name,token=state['token'],epoch=state['epoch']))
            saved=DIRECTORY/(name+'-intent.json')
            if saved.exists():
                intent=json.loads(saved.read_text())['intent']
                call('dp','recover',{k:intent[k] for k in ('group','token','boot_id')})
            return dict(stopped=True,**status())
        observed=status()['groups'].get(name,{})
        if observed.get('fresh'):
            if observed.get('running_revision')==payload['running_revision'] and observed.get('control_only')==(operation=='negotiate') and observed.get('offload_requested',False)==(operation=='offload'):
                return observed
            raise ValueError('Deactivate the current aggregate owner before changing activation mode')
        dp=call('dp','status',{});cp=call('cp','status',{})
        selected=prepare(raw,name,operation=='negotiate',dp['boot_id'],offload=operation=='offload')
        if any(g.get('phase')!='stopped' and (n==name or set(g['ports'])&set(selected['intent']['members'])) for n,g in cp['groups'].items()):
            raise ValueError('CP aggregate ownership must be recovered before activation')
        if action=='validate':return dict(validated=True,group=name,control_only=operation=='negotiate',hardware_offload=False)
        if RUNNING.read_bytes()!=raw:raise ValueError('Running configuration changed during validation')
        from policy_guard import before_commit
        before_commit(raw)
        atomic(DIRECTORY/'revision.json',{'revision':revision()+1})
        selected['epoch']=cp['epoch'];atomic(DIRECTORY/(name+'-intent.json'),selected)
        subprocess.run(['systemctl','start','ffn-aggregate@'+name],check=True,timeout=15)
        fcntl.flock(lock,fcntl.LOCK_UN)
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            result=status()['groups'].get(name,{})
            if result.get('activation_id')==selected['activation_id'] and result.get('fresh'):
                if result['state'] in ('negotiating','active','control-only','awaiting-address'):return result
                if result['state']=='failed':return result
            if result.get('activation_id')==selected['activation_id'] and result.get('error'):return result
            time.sleep(.2)
        return dict(group=name,activation_id=selected['activation_id'],state='starting',accepted=True,applied=False,
                    detail='MP supervisor started; follow aggregate status for CP/DP activation')


def supervise(name):
    name=valid_name(name);selected=json.loads((DIRECTORY/(name+'-intent.json')).read_text());intent=selected['intent']
    path=DIRECTORY/(name+'-status.json');dp=None;cp=None;prepared=False
    state=dict(group=name,activation_id=selected.get('activation_id',intent['token']),token=intent['token'],running_revision=selected['running_revision'],control_only=intent['control_only'],
        offload_requested=intent['offload'],pid=os.getpid(),process_start=Path('/proc/self/stat').read_text().rsplit(') ',1)[1].split()[19],
        boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),state='starting',applied=False)
    def save():state['updated_monotonic']=time.monotonic();atomic(path,state)
    def halt(*_):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,halt);save()
    identity=dict(group=name,token=intent['token'],epoch=selected['epoch'])
    try:
        selected=resume_selection(name);intent=selected['intent']
        identity=dict(group=name,token=intent['token'],epoch=selected['epoch'])
        state.update(token=intent['token'],running_revision=selected['running_revision'])
        save()
        if hashlib.sha256(RUNNING.read_bytes()).hexdigest()!=selected['running_revision']:raise ValueError('Committed configuration changed before startup')
        dp=subprocess.Popen(command('dp','serve'),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=None,start_new_session=True,bufsize=0)
        dp.stdin.write((json.dumps(intent)+'\n').encode());dp.stdin.flush()
        ready,_,_=select.select([dp.stdout],[],[],12)
        if not ready:raise RuntimeError('DP aggregate owner did not initialize')
        first=dp.stdout.readline(65537)
        if len(first)>65536 or not first.endswith(b'\n'):raise RuntimeError('Invalid DP startup frame')
        row=json.loads(first)
        if row.get('token')!=intent['token'] or row.get('boot_id')!=intent['boot_id']:raise RuntimeError('DP startup identity mismatch')
        # Cleanup uses this token even when the prepare response is lost.
        prepared=True
        remote('cp','prepare',dict(identity,ports=intent['members'],speeds=selected['speeds'],offload=intent['offload']))
        cp=subprocess.Popen(command('cp','stream'),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=None,start_new_session=True,bufsize=0)
        for process in (cp,dp):os.set_blocking(process.stdout.fileno(),False)
        buffers={cp.stdout:b'',dp.stdout:b''};sent=None;sequence=0;next_send=0;last_dp=time.monotonic()
        state.update(state='negotiating',dataplane=row,dp_received_monotonic=last_dp);save()
        while True:
            now=time.monotonic()
            if any(p.poll() is not None for p in (cp,dp)):raise RuntimeError('Aggregate agent exited')
            if now-last_dp>5:raise RuntimeError('DP observation expired')
            raw=RUNNING.read_bytes();current_revision=hashlib.sha256(raw).hexdigest()
            if current_revision!=selected['running_revision']:
                groups=[g for g in plan(raw)['aggregates'] if g['ae_name']==name]
                if len(groups)!=1 or link_revision(groups[0])!=selected.get('link_revision'):
                    raise RuntimeError('Aggregate members or link settings changed; reconciliation required')
                try:
                    update=prepare(raw,name,intent['control_only'],intent['boot_id'],intent['offload'])
                    intent.update(network=update['intent']['network'],lldp=update['intent']['lldp'])
                    state.pop('configuration_error',None)
                except ValueError as error:
                    # Unsupported network settings must not kill LACP or retain
                    # potentially stale interface-local access permissions.
                    from ffn_interface_management import profile
                    intent['network']=dict(intent['network'],enabled=False,addresses=[],dhcp=False,units=[],management=profile(None,''))
                    state['configuration_error']=str(error)
                selected['running_revision']=current_revision;state['running_revision']=current_revision
            if sent is not None and now-sent>5:raise RuntimeError('CP link observation expired')
            if sent is None and now>=next_send:
                sequence+=1;sent=now
                latest=state['dataplane']
                egress=latest.get('distributing',[]) if now-last_dp<3 else []
                cp.stdin.write((json.dumps(dict(sequence=sequence,payload=dict(identity,egress=egress)))+'\n').encode());cp.stdin.flush()
            ready,_,_=select.select(list(buffers),[],[],.1)
            for stream in ready:
                data=os.read(stream.fileno(),65536)
                if not data:raise RuntimeError('Aggregate control stream closed')
                buffers[stream]+=data
                if len(buffers[stream])>262144:raise RuntimeError('Aggregate agent output exceeds limit')
                while b'\n' in buffers[stream]:
                    line,buffers[stream]=buffers[stream].split(b'\n',1);row=json.loads(line)
                    if row.get('error'):raise RuntimeError(row['error'])
                    if row.get('token')!=intent['token']:raise RuntimeError('Aggregate owner token changed')
                    if stream is cp.stdout:
                        if row.get('sequence')!=sequence or sent is None or row.get('epoch')!=selected['epoch'] or row.get('phase')!='active':raise RuntimeError('CP observation identity changed')
                        age=time.monotonic()-sent
                        if age>=5:raise RuntimeError('CP response too old')
                        config=dict(revision=network_revision(intent),network=intent['network'],lldp=intent['lldp'])
                        dp.stdin.write((json.dumps(dict(token=intent['token'],sequence=sequence,age_seconds=age,links=row['links'],offload=row.get('offload'),config=config))+'\n').encode());dp.stdin.flush()
                        state['hardware']=row;sent=None;next_send=time.monotonic()+.5
                    else:
                        if row.get('boot_id')!=intent['boot_id']:raise RuntimeError('DP lifetime changed')
                        last_dp=time.monotonic();state['dataplane']=row;state['dp_received_monotonic']=last_dp
                        acknowledged=row.get('configuration_revision')==network_revision(intent)
                        state['configuration_revision']=network_revision(intent)
                        ready=row.get('attachment_ready') or (not intent['network'].get('enabled',True) and row.get('distributing'))
                        state['state']='control-only' if intent['control_only'] else 'apply-failed' if row.get('network_error') or state.get('configuration_error') else 'reconciling' if not acknowledged else ('active' if row.get('network_ready') else 'awaiting-address') if ready else 'negotiating'
                        state['applied']=bool(acknowledged and ready and row.get('network_ready')) and not intent['control_only'] and not state.get('configuration_error')
                    save()
    except KeyboardInterrupt:state.update(state='stopping',applied=False);save()
    except BaseException as error:state.update(state='failed',error=str(error),applied=False);save()
    finally:
        # Closing DP input removes collection/distribution before CP withdrawal.
        if dp and dp.stdin:
            with contextlib.suppress(OSError):dp.stdin.close()
        if cp and cp.stdin:
            with contextlib.suppress(OSError):cp.stdin.close()
        if prepared:
            try:remote('cp','stop',identity)
            except Exception as error:state['cleanup_error']=str(error)
        for process in (dp,cp):
            if process:
                try:process.wait(timeout=12)
                except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait();state['cleanup_error']='Remote shutdown did not acknowledge; inspect CP/DP status'
        if state['state']!='failed':state['state']='stopped'
        state['applied']=False;save()
    return state['state']!='failed'


if __name__=='__main__':
    if sys.argv[1]=='serve':raise SystemExit(0 if supervise(sys.argv[2]) else 1)
    else:print(json.dumps(execute(sys.argv[1],json.load(sys.stdin))))
