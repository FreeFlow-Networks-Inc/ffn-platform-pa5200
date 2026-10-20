#!/usr/bin/env python3
"""Fixed MP daemon adapters. Only the daemon executes these hardware helpers."""
import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path
from hardware_backend import Controller, COMMANDS, HTTPException

FIELDS={'phy':{'revision','phy','speed'},'bcm':{'revision','operation','acknowledge_link_outage'},'network':{'revision','ports','routes','vrfs','rules'},
        'fe100-policy':{'revision','digest'},'dhcp':{'revision','servers','configuration'},'protect':{'revision','protect','configuration'},'lldp':{'revision','lldp','configuration'},'service-routes':{'revision','routes','configuration'},
        'overlay':{'revision','links'},'inspection':{'revision','mode','ports','literal','detectors'},
        'faceplate':{'revision','port','enabled','speed','restart_autoneg','restore_pair_map'},'thermal':{'revision','operation'}}


def require_front_mode(port,config=Path('/var/lib/ffn-ngfw/config/running-config.xml')):
    """A hardware enable cannot bypass the committed interface mode."""
    from aggregate_config import parse
    root=parse(config.read_bytes())
    entry=root.find("./devices/entry[@name='localhost.localdomain']/network/interface/ethernet/entry[@name='ethernet1/%d']"%port)
    if (entry is None or entry.findtext('link-state','auto')=='down' or
        not (any(entry.find(mode) is not None for mode in ('layer3','layer2','virtual-wire','tap','ha','decrypt-mirror')) or entry.findtext('aggregate-group','').strip())):
        raise ValueError('Front interface is None/disabled; select and commit an interface mode before enabling its link')


# ---- tracked routes: the convergence check for the dataplane's link-tracked routes ----
# The core's ffn_convergence.py asks the daemon for `tracked-routes status` and reports it
# as the `tracked-routes` subsystem; ffn-alerts pages on `failed`. One ssh round trip to the
# DP, read-only: the tracker's state (core ffn_static_routes.py, every second), the link
# observation it judges by (octeon/debian/ffn_network.py route_link: 60 s lease, this DP
# boot), the network journal (which routes are tracked), and the DP's clock and boot to
# judge the ages. 2026-10-20: both default routes were withdrawn for fifteen minutes with
# `hardware-link-observation-stale` and nothing reported it.
TRACKED_ROUTES_SPLIT='FFN_TRACKED_ROUTES_SPLIT'
TRACKED_ROUTES_COLLECT=('cat /run/ffn-static-routes.json 2>/dev/null; echo %s; cat /run/ffn-route-links.json 2>/dev/null; echo %s; '
                        'cat /etc/ffn/network.json 2>/dev/null; echo %s; cat /proc/uptime; cat /proc/sys/kernel/random/boot_id')%((TRACKED_ROUTES_SPLIT,)*3)
TRACKER_FRESH_SECONDS=30       # the tracker ticks every second; its stop hook withdraws every tracked route
OBSERVATION_LEASE_SECONDS=60   # ffn_network.py route_link: older than this and every tracked route is withdrawn
FEED_REASONS={'hardware-link-observation-stale','hardware-link-observation-unavailable'}   # the observation, not the link
WAITING_REASONS={'hardware-link-down','link-down','aggregate-no-live-member','hardware-link-unavailable','path-down','monitor-stale'}
FEED_UNIT='ffn-route-link-feed.service'   # the MP writer of the observation; ffn-portd links is the other
DP_ARGV=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=5',
         '-o','UserKnownHostsFile=/etc/ffn-ngfw/plane_boot_known_hosts',
         '-o','ProxyCommand=ssh -F /etc/ffn-ngfw/ssh-cp.conf -W %h:%p ffn-cp','root@127.1.2.2']


def dp_collect(script=TRACKED_ROUTES_COLLECT,argv=DP_ARGV):
    result=subprocess.run(argv+[script],text=True,capture_output=True,timeout=25)
    if result.returncode: raise RuntimeError('Dataplane collection failed: '+result.stderr[-300:])
    return result.stdout


def feed_unit_state(run=subprocess.run,unit=FEED_UNIT):
    """systemd's word for the MP feed (active, inactive, failed, ...), or None when it cannot say."""
    try: result=run(['systemctl','is-active',unit],capture_output=True,text=True,timeout=10)
    except (OSError,subprocess.SubprocessError): return None
    return (result.stdout or '').strip() or None


def parse_tracked_routes(output):
    """(tracker state, link observation, network journal, DP uptime, DP boot id) from one collection."""
    parts=output.split(TRACKED_ROUTES_SPLIT+'\n')
    if len(parts)!=4: raise ValueError('Dataplane collection is incomplete')
    def document(text):
        try: value=json.loads(text)
        except ValueError: return None
        return value if isinstance(value,dict) else None
    tail=parts[3].split()
    if len(tail)<3: raise ValueError('Dataplane clock or boot identity missing')
    return document(parts[0]),document(parts[1]),document(parts[2]),float(tail[0]),tail[-1]


def route_text(route):
    text=str(route.get('dst','?'))
    if route.get('via'): text+=' via '+str(route['via'])
    if route.get('dev'): text+=' dev '+str(route['dev'])
    return text


def tracked_routes_report(state,observation,network,uptime,dp_boot,feed_state=None):
    """Each link-tracked route's installed/reason on the dataplane, judged as a convergence subsystem.

    failed     a route is withdrawn because the hardware link observation is stale or missing
               (the feed, not the link), the tracker itself has stopped ticking, or a route
               cannot be installed as committed;
    pending    a route waits for its link, path or aggregate member, or the tracker has not yet
               reported for this DP boot or the current network revision;
    converged  every tracked route is installed (or none is tracked)."""
    tracked=[r for r in (network or {}).get('routes',[]) if isinstance(r,dict) and r.get('track_link')]
    revision=(network or {}).get('revision')
    notes=[]
    present=isinstance(observation,dict)
    obs_age=obs_boot=None
    if present:
        try: obs_age=uptime-float(observation['observed']);obs_boot=observation.get('boot_id')==dp_boot
        except (KeyError,TypeError,ValueError): present=False
    if present:
        fresh=obs_boot and 0<=obs_age<=OBSERVATION_LEASE_SECONDS
        notes.append('link observation %d s old%s%s'%(max(0,round(obs_age)),'' if obs_boot else ' from another DP boot',
                                                     '' if fresh else ' (stale: tracked routes withdrawn)'))
    else: notes.append('no link observation on the dataplane')
    if feed_state is not None: notes.append('MP feed %s %s'%(FEED_UNIT,feed_state))
    summary_of=lambda n:'%d tracked route(s)'%n
    def report(verdict,summary,rows,routes):
        return dict(state=verdict,summary=summary,details=(rows+notes)[:8],routes=routes,
                    tracker=dict(reported=isinstance(state,dict) and state.get('boot')==dp_boot,
                                 age_seconds=None if not isinstance(state,dict) or state.get('boot')!=dp_boot else state.get('age_seconds'),
                                 revision=None if not isinstance(state,dict) else state.get('revision')),
                    observation=dict(present=present,age_seconds=None if not present else round(obs_age,1),boot_matches=obs_boot),
                    feed=dict(unit=FEED_UNIT,state=feed_state))
    if not isinstance(state,dict) or state.get('boot')!=dp_boot or not isinstance(state.get('routes'),dict):
        if not tracked: return report('converged','No link-tracked routes on the dataplane',[],[])
        rows=[route_text(r)+': not yet judged' for r in tracked]
        return report('pending','Route tracker has not reported on this dataplane boot',rows,
                      [dict(dst=r.get('dst'),dev=r.get('dev'),via=r.get('via'),metric=r.get('metric'),installed=None,reason='tracker-not-reported') for r in tracked])
    routes=[];rows=[]
    for record in state['routes'].values():
        route=record.get('route') if isinstance(record,dict) and isinstance(record.get('route'),dict) else {}
        installed=record.get('installed') is True if isinstance(record,dict) else False
        reason=str((record or {}).get('reason','unknown'))[:64] if isinstance(record,dict) else 'unknown'
        routes.append(dict(dst=route.get('dst'),dev=route.get('dev'),via=route.get('via'),metric=route.get('metric'),installed=installed,reason=reason))
        rows.append(route_text(route)+(': installed' if installed else ': withdrawn (%s)'%reason))
    try: age=uptime-float(state['observed'])
    except (KeyError,TypeError,ValueError): age=None
    state['age_seconds']=None if age is None else round(age,1)
    if age is None or not 0<=age<=TRACKER_FRESH_SECONDS:
        return report('failed','Route tracker stalled: last tick %s s ago'%('?' if age is None else int(age)),rows,routes)
    if network is not None and state.get('revision')!=revision:
        return report('pending','Route tracker has not applied network revision %s'%revision,rows,routes)
    withdrawn=[r for r in routes if not r['installed']]
    feed=[r for r in withdrawn if r['reason'] in FEED_REASONS]
    waiting=[r for r in withdrawn if r['reason'] in WAITING_REASONS]
    other=[r for r in withdrawn if r not in feed and r not in waiting]
    if feed: return report('failed','%s withdrawn: hardware link observation stale on the dataplane'%summary_of(len(feed)),rows,routes)
    if other: return report('failed','%s cannot be installed as committed'%summary_of(len(other)),rows,routes)
    if waiting: return report('pending','%d of %s waiting for a link'%(len(waiting),summary_of(len(routes))),rows,routes)
    if not routes: return report('converged','No link-tracked routes on the dataplane',rows,routes)
    return report('converged','%s installed'%summary_of(len(routes)),rows,routes)


async def execute(resource, action, payload, backend=None, collect=None, unit_state=None):
    backend=backend or Controller()
    if resource=='tracked-routes' and action=='status':
        if payload: raise ValueError('status takes no payload')
        collected=await asyncio.to_thread(collect or dp_collect)
        feed_state=await asyncio.to_thread(unit_state or feed_unit_state)
        return tracked_routes_report(*parse_tracked_routes(collected),feed_state=feed_state)
    if resource=='route-links' and action=='refresh':
        if payload: raise ValueError('Observation refresh takes no payload')
        observed=await backend.run('network','status')
        faceplate=await backend.run('faceplate','status')
        links={'p'+str(p['port']):p.get('available') is True and p.get('enabled') is True and p.get('link') is True for p in faceplate['ports']}
        return await backend.run('network','health',dict(revision=observed['config']['revision'],boot_id=observed['boot_id'],links=links))
    if resource=='network' and action in ('validate','apply') and payload.get('refresh_route_links') is True:
        if set(payload)!={'revision','refresh_route_links'}: raise ValueError('Invalid link refresh')
        observed=await backend.run('network','status')
        if observed['config']['revision']!=payload['revision']: raise ValueError('Network revision changed')
        if action=='validate': return {'validated':True}
        faceplate=await backend.run('faceplate','status')
        links={'p'+str(p['port']):p.get('available') is True and p.get('enabled') is True and p.get('link') is True for p in faceplate['ports']}
        return await backend.run('network','health',dict(revision=payload['revision'],boot_id=observed['boot_id'],links=links))
    if resource == 'lacp':
        if action == 'status':
            if payload: raise ValueError('status takes no payload')
            return await backend.run('lacp','status')
        if action not in ('apply','validate'): raise ValueError('unsupported LACP action')
        data=dict(payload)
        operation=data.pop('operation', None)
        fields={'revision','groups'} if operation=='set' else {'revision','group'}
        if operation not in ('set','activate','deactivate') or set(data)!=fields:
            raise ValueError('invalid LACP operation')
        observed=await backend.run('lacp','status')
        if type(data['revision']) is not int or data['revision']!=observed['config']['revision']:
            raise ValueError('revision conflict')
        if operation=='activate' and not observed['capabilities']['activation_supported']:
            raise ValueError('LACP backend is not qualified')
        if action=='validate': return {'validated':True}
        return await backend.run('lacp',operation,data)
    if resource=='service-routes':
        # Two halves: the dataplane installer (relayed) and the MP's own policy-routing rules.
        import sys as _sys
        _sys.path[:0]=['/opt/ffn-ngfw-v2','/opt/ffn-ngfw']
        import ffn_service_routes as _routes
        if action=='status':
            if payload: raise ValueError('status takes no payload')
            observed=await backend.run('service-routes','status')
            try: observed['mp']=_routes.mp_status()
            except Exception: observed['mp']=None
            return observed
        if action not in ('apply','validate') or set(payload)-FIELDS[resource]: raise ValueError('unsupported fields')
        if action=='validate': return await backend.run('service-routes','validate',payload)
        observed=await backend.run('service-routes','apply',payload)
        observed['mp']=_routes.mp_apply(_routes.validate_intent(dict(routes=observed['config'].get('routes',{}))))
        return observed
    if resource=='service-routes':
        # Two halves: the dataplane installer (relayed) and the MP's own policy-routing rules.
        import sys as _sys
        _sys.path[:0]=['/opt/ffn-ngfw-v2','/opt/ffn-ngfw']
        import ffn_service_routes as _routes
        if action=='status':
            if payload: raise ValueError('status takes no payload')
            observed=await backend.run('service-routes','status')
            try: observed['mp']=_routes.mp_status()
            except Exception: observed['mp']=None
            return observed
        if action not in ('apply','validate') or set(payload)-FIELDS[resource]: raise ValueError('unsupported fields')
        if action=='validate': return await backend.run('service-routes','validate',payload)
        observed=await backend.run('service-routes','apply',payload)
        observed['mp']=_routes.mp_apply(_routes.validate_intent(dict(routes=observed['config'].get('routes',{}))))
        return observed
    if resource=='service-routes':
        # Two halves: the dataplane installer (relayed) and the MP's own policy-routing rules.
        import sys as _sys
        _sys.path[:0]=['/opt/ffn-ngfw-v2','/opt/ffn-ngfw']
        import ffn_service_routes as _routes
        if action=='status':
            if payload: raise ValueError('status takes no payload')
            observed=await backend.run('service-routes','status')
            try: observed['mp']=_routes.mp_status()
            except Exception: observed['mp']=None
            return observed
        if action not in ('apply','validate') or set(payload)-FIELDS[resource]: raise ValueError('unsupported fields')
        if action=='validate': return await backend.run('service-routes','validate',payload)
        observed=await backend.run('service-routes','apply',payload)
        observed['mp']=_routes.mp_apply(_routes.validate_intent(dict(routes=observed['config'].get('routes',{}))))
        return observed
    if resource=='service-routes':
        # Two halves: the dataplane installer (relayed) and the MP's own policy-routing rules.
        import sys as _sys
        _sys.path[:0]=['/opt/ffn-ngfw-v2','/opt/ffn-ngfw']
        import ffn_service_routes as _routes
        if action=='status':
            if payload: raise ValueError('status takes no payload')
            observed=await backend.run('service-routes','status')
            try: observed['mp']=_routes.mp_status()
            except Exception: observed['mp']=None
            return observed
        if action not in ('apply','validate') or set(payload)-FIELDS[resource]: raise ValueError('unsupported fields')
        if action=='validate': return await backend.run('service-routes','validate',payload)
        observed=await backend.run('service-routes','apply',payload)
        observed['mp']=_routes.mp_apply(_routes.validate_intent(dict(routes=observed['config'].get('routes',{}))))
        return observed
    if action in ('apply','validate'):
        if resource not in FIELDS or set(payload)-FIELDS[resource]: raise ValueError('unsupported fields')
        if type(payload.get('revision')) is not int or payload['revision']<0: raise ValueError('revision required')
        if resource=='thermal':
            if set(payload)!=FIELDS[resource] or payload['operation'] not in ('auto','full') or payload['revision']!=0:
                raise ValueError('invalid thermal operation')
        else:
            observed=await backend.run(resource,'status')
            revision=observed.get('config',observed).get('revision')
            if revision!=payload['revision']: raise ValueError('revision conflict; refresh state')
            if resource=='fe100-policy':
                if set(payload)!=FIELDS[resource] or not isinstance(payload['digest'],str) or not re.fullmatch('[0-9a-f]{64}',payload['digest']):
                    raise ValueError('exact candidate digest required')
            if resource=='phy':
                if set(payload)!=FIELDS['phy'] or type(payload['phy']) is not int or payload['phy'] not in range(16,20) or payload['speed'] not in ('auto','100','1000','10000'):
                    raise ValueError('Invalid PHY configuration')
                if observed.get('saved',{}).get('pending') or not observed['phys'][payload['phy']-16].get('ready'):
                    raise ValueError('PHY unavailable or operation pending')
            if resource=='bcm':
                if set(payload)!=FIELDS['bcm'] or payload.get('operation') not in ('start','stop','restart') or payload.get('acknowledge_link_outage') is not True:
                    raise ValueError('Invalid BCM service request')
                if not observed.get('operation_complete'):raise ValueError('BCM operation still pending')
            if resource=='faceplate':
                if (not {'port','revision'}<=set(payload) or not {'enabled','speed','restart_autoneg','restore_pair_map'}&set(payload) or type(payload['port']) is not int or
                        not 1<=payload['port']<=24 or ('enabled' in payload and type(payload['enabled']) is not bool)):
                    raise ValueError('invalid faceplate change')
                if payload.get('enabled') is True:require_front_mode(payload['port'])
                port=next((p for p in observed.get('ports',[]) if p['port']==payload['port']),{})
                if port.get('media')=='copper' and (port.get('admin_configuration') is False or port.get('phy_pending')):
                    raise ValueError('Copper control unavailable or pending')
                if 'restart_autoneg' in payload and (payload['restart_autoneg'] is not True or set(payload)!={'revision','port','restart_autoneg'}
                        or not port.get('renegotiate_configuration') or not port.get('enabled') or observed.get('saved',{}).get('pending')):
                    raise ValueError('Renegotiation requires an enabled, ready copper port and a standalone request')
                if 'restore_pair_map' in payload and (payload['restore_pair_map'] is not True or set(payload)!={'revision','port','restore_pair_map'}
                        or not port.get('pair_map_recovery') or not port.get('enabled') or observed.get('saved',{}).get('pending')):
                    raise ValueError('Pair map recovery requires an enabled, down copper port and a standalone request')
                if 'speed' in payload:
                    if not port.get('speed_configuration') or payload['speed'] not in ['auto']+[str(v) for v in port.get('supported_speeds',[])]:
                        raise ValueError('Unsupported link speed')
            if resource=='inspection':
                from ffn_inspection import validate
                validate(payload)
        if action=='validate':
            if resource in ('network','dhcp','protect','lldp'):return await backend.run(resource,'validate',payload)
            return {'validated':True}
        actual='patch' if resource=='network' else 'apply' if resource in ('dhcp','protect','lldp') else payload['operation'] if resource=='thermal' else 'set'
        return await backend.run(resource,actual,None if resource=='thermal' else payload)
    if action not in ('status','lookup') or (resource,action) not in COMMANDS:
        raise ValueError('unsupported operation')
    if action=='status' and payload: raise ValueError('status takes no payload')
    if action=='lookup' and (set(payload)-{'dst','vrf'} or not isinstance(payload.get('dst'),str)):
        raise ValueError('invalid lookup')
    return await backend.run(resource,action,payload or None)


def error_response(error):
    """The text a failed adapter run reports.

    The adapter's own validation messages (ValueError) and hardware_backend's
    fixed controller texts (HTTPException.detail) carry no SSH diagnostics or
    configuration contents, so clients may see them: a revision conflict is
    retried by the caller and a timeout is reported as such. Anything else
    stays generic."""
    if isinstance(error,ValueError):return str(error)[:512] or 'invalid request'
    if isinstance(error,HTTPException):return str(error.detail)[:512]
    return 'MP hardware adapter rejected or could not complete the operation'


if __name__=='__main__':
    try:
        result=asyncio.run(execute(sys.argv[1],sys.argv[2],json.load(sys.stdin)))
        print(json.dumps(result))
    except Exception as error:
        # Do not send SSH diagnostics or configuration contents to clients.
        print(json.dumps({'error':error_response(error)}))
        sys.exit(2)
