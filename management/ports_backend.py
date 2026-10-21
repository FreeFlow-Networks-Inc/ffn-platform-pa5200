#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""The MP's one resource for every faceplate port: the table the DP owner reconciles.

`ffn-portd serve` owns ports 1..24 from the dataplane's boot (management/PORT-TABLE.md).
This resource derives what every port should be from the running configuration, keeps the
CP owner of every layer-3 port in step (the port redirect `ffn-portctl` holds for ports 2..24,
the WAN forwarding owner for port 1), pushes the whole table to the DP in one request, and
reads back what the daemon holds. The per-port operations the WebUI pages use (attach,
detach, the WAN qualification probe) and the aggregate lifecycle (activate, deactivate,
recover) are served here too, by the modules that always served them.

Operations (`apply`, after `validate`):
  sync        {operation, running_revision, revision}: the commit's reconciliation, one request
  attach      {operation, port, running_revision, revision}: one port, as physical-ports did
  detach      same, the port withdrawn
  probe       {operation, revision, expected_boot_id}: WAN wire qualification (wan_backend)
  activate | deactivate | recover  {operation, group, running_revision, revision}: aggregate_activation
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from xml.etree import ElementTree as ET
sys.path.extend(['/opt/ffn-ngfw','/opt/ffn-ngfw-v2'])
from aggregate_activation import CP,DP

RUNNING=Path('/var/lib/ffn-ngfw/config/running-config.xml')
PORTS=range(1,25)
ATTACH_WAIT=12          # seconds the sync waits for the daemon to attach every l3 row
POLL=0.5
CP_PORT=('if [ -x /usr/local/sbin/ffn-portctl ]; then exec /usr/local/sbin/ffn-portctl %s; fi; '
         'exec python3 /usr/local/sbin/ffn_physical_forwarding.py %s')


def command(role,operation):
    """The plane command one operation runs; the payload goes on stdin as JSON."""
    if role=='cp':
        if operation in ('port-status','port-start','port-stop'):
            verb=operation[5:];return CP+[CP_PORT%(verb,verb)]
        if operation in ('wan-status','wan-start','wan-stop','wan-recover'):
            return CP+['python3 /usr/local/sbin/ffn_wan_forwarding.py '+operation[4:]]
    if role=='dp':
        if operation in ('table-apply','table-status'):return DP+['/usr/local/sbin/ffn-portd table '+operation[6:]]
        if operation=='wan-status':return DP+['python3 /usr/local/sbin/ffn_wan_probe.py --status']
        if operation=='fabric-reconcile':return DP+['python3 /usr/local/sbin/ffn_dp_packet_init.py reconcile']
    raise ValueError('Unknown port operation '+role+'/'+operation)


def remote(role,operation,payload):
    result=subprocess.run(command(role,operation),input=json.dumps(payload),text=True,capture_output=True,timeout=60)
    try:value=json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {}
    except ValueError:value={}
    if result.returncode or not isinstance(value,dict) or value.get('error'):
        detail=value.get('error') if isinstance(value,dict) and value.get('error') else (result.stdout[-600:].strip() or result.stderr[-600:].strip() or 'exit status %d'%result.returncode)
        raise RuntimeError(role+' '+operation+' failed: '+str(detail)[:600])
    return value


# ---------------------------------------------------------------- the table from the configuration

def port_number(name):
    match=re.fullmatch(r'ethernet1/([1-9]|1[0-9]|2[0-4])',name or '')
    return int(match[1]) if match else None


def desired_table(raw):
    """{port: {mode, group?}} for ports 1..24 from the running configuration.

    aggregate-group aeN         -> aggregate (the port is reserved for its lease)
    layer3 with an address or a management profile, link-state not down -> l3
    anything else (None, link-state down, an empty layer3 placeholder)  -> disabled
    """
    from aggregate_config import parse
    root=parse(raw)
    try:
        from ffn_interface_addresses import resolved_config
        root=resolved_config(root)
    except ImportError:pass
    device=root.find("./devices/entry[@name='localhost.localdomain']")
    rows={port:{'mode':'disabled'} for port in PORTS}
    if device is None:return rows
    for entry in device.findall('./network/interface/ethernet/entry'):
        port=port_number(entry.get('name'))
        if port is None:continue
        group=(entry.findtext('aggregate-group') or '').strip()
        if group:
            if re.fullmatch(r'ae(?:[1-9]|1[0-2])',group):rows[port]={'mode':'aggregate','group':group}
            continue
        l3=entry.find('layer3')
        if l3 is None or entry.findtext('link-state','auto')=='down':continue
        addresses=[e.get('name') for e in l3.findall('./ip/entry') if e.get('name')]
        profile=(l3.findtext('interface-management-profile') or '').strip()
        if addresses or profile:rows[port]={'mode':'l3'}
    return rows


def table_request(rows,boot_id,generation):
    return dict(boot_id=boot_id,generation=generation,ports={str(port):row for port,row in rows.items()})


# ---------------------------------------------------------------- the CP owners

def cp_port_state(port,call):
    if port==1:
        current=call('cp','wan-status',{})
        state=current.get('state') or {}
        return dict(revision=current.get('revision'),epoch=current.get('epoch'),ready=bool(current.get('ready',{}).get('1')) if isinstance(current.get('ready'),dict) else bool(state.get('enabled')),
                    dp_boot_id=state.get('dp_boot_id'),enabled=bool(state.get('enabled')),pending=state.get('pending'),state=state,raw=current)
    cp=call('cp','port-status',{'port':port})
    state=cp.get('state') or {}
    return dict(epoch=cp.get('epoch'),ready=bool(cp.get('ready')),dp_boot_id=state.get('dp_boot_id'),enabled=bool(state.get('enabled',cp.get('ready'))),
                pending=state.get('pending'),state=state,raw=cp)


def cp_port_start(port,cp,dp_boot,call):
    """Start the CP owner of one port for this DP boot; returns the CP's readback."""
    if port==1:
        raw=cp['raw'];state=cp['state']
        if state.get('pending') or state.get('epoch')!=raw.get('epoch') or (state.get('enabled') and state.get('dp_boot_id')!=dp_boot):
            # A DP-only reboot retains the CP switch redirect. Withdraw that old
            # attachment before commissioning the new DP lifetime (wan_backend).
            raw=call('cp','wan-recover',{'revision':raw['revision']})
        result=call('cp','wan-start',{'revision':raw['revision'],'dp_boot_id':dp_boot})
        if not (result.get('ready') or {}).get('1'):raise RuntimeError('WAN redirect readback incomplete')
        return result
    result=call('cp','port-start',{'port':port,'epoch':cp['epoch'],'dp_boot_id':dp_boot})
    if not result.get('ready'):raise RuntimeError('port %d CP redirect readback incomplete'%port)
    return result


def cp_port_stop(port,cp,dp_boot,call):
    if port==1:return call('cp','wan-stop',{'revision':cp['raw']['revision']})
    return call('cp','port-stop',{'port':port,'epoch':cp['epoch'],'dp_boot_id':dp_boot})


def cp_started_for(cp,dp_boot):
    return bool(cp['ready']) and cp['dp_boot_id']==dp_boot and not cp.get('pending')


# ---------------------------------------------------------------- the resource

def dp_table(call):
    status=call('dp','table-status',{})
    if status.get('daemon') is False or not isinstance(status.get('ports'),dict) or not status.get('boot_id'):
        raise RuntimeError('DP port owner (ffn-portd) is not serving')
    return status


def status(call=remote,aggregates=True):
    table=dp_table(call)
    cp={}
    for port in PORTS:
        row=table['ports'].get(str(port)) or {}
        if row.get('desired')=='l3':
            try:cp[str(port)]=cp_port_state(port,call)
            except (RuntimeError,ValueError,KeyError) as error:cp[str(port)]={'error':str(error)[:300]}
    result=dict(config={'revision':table.get('revision',0)},boot_id=table['boot_id'],generation=table.get('generation'),
                running_revision=hashlib.sha256(RUNNING.read_bytes()).hexdigest() if RUNNING.exists() else None,
                dp=table,cp=cp,backend='ffn-portd')
    if aggregates:
        import asyncio
        from aggregate_backend import execute as aggregate_report
        try:result.update(aggregates_report=asyncio.run(aggregate_report('status',{})))
        except Exception as error:result.update(aggregates_report={'error':str(error)[:300]})
        report=result['aggregates_report']
        for key in ('aggregates','activation','revision','orphan_members','running_aggregates'):
            if key in report:result[key]=report[key]
    return result


def wait_attached(wanted,boot_id,call,seconds=ATTACH_WAIT):
    """The table status once every wanted l3 row is attached, or when the wait runs out."""
    deadline=time.monotonic()+seconds;table=None
    while True:
        table=dp_table(call)
        if table['boot_id']!=boot_id:raise RuntimeError('DP boot identity changed during the port sync')
        rows=table['ports']
        pending=[p for p in wanted if not (rows.get(str(p)) or {}).get('attached')]
        if not pending or time.monotonic()>=deadline:return table,pending
        time.sleep(POLL)


def sync(payload,call=remote,drain=None):
    """The commit's reconciliation: the CP owners in step, one table push, one readback."""
    raw=RUNNING.read_bytes()
    if payload['running_revision']!=hashlib.sha256(raw).hexdigest():raise ValueError('Running configuration changed; refresh')
    before=dp_table(call);boot=before['boot_id']
    if type(payload['revision']) is not int or payload['revision']!=before.get('revision',0):raise ValueError('Port table revision changed; refresh')
    rows=desired_table(raw);errors={};started={}
    cp={};want_l3=[p for p in PORTS if rows[p]['mode']=='l3']
    for port in PORTS:
        have=(before['ports'].get(str(port)) or {})
        if rows[port]['mode']=='l3' or have.get('desired')=='l3' or have.get('attached'):
            try:cp[port]=cp_port_state(port,call)
            except (RuntimeError,ValueError,KeyError) as error:
                errors[port]='CP owner unavailable: '+str(error)[:300]
    # Port 1's attachment changing drains the policy barrier first, as the WAN owner always did.
    wan_changes=rows[1]['mode']!=(before['ports'].get('1') or {}).get('desired','disabled')
    if wan_changes and drain is not None:drain(json.dumps(dict(operation='sync',running_revision=payload['running_revision']),sort_keys=True).encode())
    for port in want_l3:
        if port in errors or port not in cp:continue
        if cp_started_for(cp[port],boot):continue
        try:cp[port]['raw']=cp_port_start(port,cp[port],boot,call);started[port]=cp[port]
        except (RuntimeError,ValueError,KeyError) as error:errors[port]='CP owner: '+str(error)[:300]
    push={port:(rows[port] if port not in errors else {'mode':'disabled'}) for port in PORTS}
    reply=call('dp','table-apply',table_request(push,boot,payload['running_revision']))
    if not reply.get('ok'):raise RuntimeError('DP refused the port table: '+str(reply.get('error'))[:300])
    for key,reason in (reply.get('rejected') or {}).items():errors[int(key)]='DP: '+str(reason)[:300]
    table,pending=wait_attached([p for p in want_l3 if p not in errors],boot,call)
    for port in pending:
        row=table['ports'].get(str(port)) or {}
        errors[port]='DP owner: '+str(row.get('reason') or 'not attached')[:300]
    # A port that did not come up, or is no longer l3, must not keep a CP redirect.
    for port,state in cp.items():
        if port in started and port in errors:
            try:cp_port_stop(port,state,boot,call)
            except (RuntimeError,ValueError,KeyError) as error:errors[port]+='; CP withdrawal failed: '+str(error)[:200]
        elif rows[port]['mode']!='l3' and (state.get('enabled') or state.get('ready')):
            try:cp_port_stop(port,state,boot,call)
            except (RuntimeError,ValueError,KeyError) as error:errors[port]='CP withdrawal failed: '+str(error)[:300]
    if RUNNING.read_bytes()!=raw:raise ValueError('Running configuration changed during the port sync')
    attached=sorted(int(k) for k,row in table['ports'].items() if row.get('attached'))
    return dict(applied=not errors,revision=table.get('revision'),boot_id=boot,attached=attached,
                errors={'ethernet1/%d'%port:reason for port,reason in sorted(errors.items())},table=table['ports'],aggregates={k:v for k,v in (table.get('aggregates') or {}).items()})


def execute(action,payload,call=remote,drain=None):
    if action=='status':
        if payload:raise ValueError('Status takes no fields')
        return status(call)
    if action=='lookup':
        if set(payload)!={'port'}:raise ValueError('lookup takes a port')
        from physical_backend import execute as physical
        return physical('lookup',payload)
    if action not in ('validate','apply') or not isinstance(payload,dict):raise ValueError('Invalid port request')
    operation=payload.get('operation')
    if operation=='sync':
        if set(payload)!={'operation','running_revision','revision'}:raise ValueError('sync takes running_revision and revision')
        if action=='validate':
            if payload['running_revision']!=hashlib.sha256(RUNNING.read_bytes()).hexdigest():raise ValueError('Running configuration changed; refresh')
            return {'validated':True}
        if drain is None:
            from policy_guard import before_commit
            drain=before_commit
        return sync(payload,call,drain)
    if operation in ('attach','detach'):
        from physical_backend import execute as physical
        from wan_backend import execute as wan
        port=payload.get('port')
        if port==1:
            request={k:v for k,v in payload.items() if k in ('operation','revision','expected_boot_id')}
            return wan(action,request)
        return physical(action,{k:v for k,v in payload.items() if k!='group'})
    if operation in ('probe','recover') and 'expected_boot_id' in payload:
        from wan_backend import execute as wan
        return wan(action,payload)
    if operation in ('activate','deactivate','recover','negotiate','offload'):
        from aggregate_activation import execute as aggregate
        return aggregate(action,payload)
    raise ValueError('Unknown port operation')


if __name__=='__main__':
    try:print(json.dumps(execute(sys.argv[1],json.load(sys.stdin))))
    except ValueError as error:
        print(json.dumps({'error':str(error)[:1024]}));sys.exit(2)
    except (RuntimeError,KeyError,subprocess.TimeoutExpired) as error:
        import syslog
        syslog.syslog(syslog.LOG_ERR,'ffn-ports-backend: '+str(error)[:1024])
        print(json.dumps({'error':str(error)[:1024]}));sys.exit(1)
