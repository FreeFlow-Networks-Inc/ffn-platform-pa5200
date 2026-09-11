# SPDX-License-Identifier: GPL-2.0-or-later
"""MP adapter for the installed PA-5220 Debian plane controllers.

No register access or remote command is supplied by an API caller. The platform
controllers remain responsible for hardware validation, persistence and rollback.
"""
import asyncio
import json
import os
import signal
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

COMMANDS = {
    ('phy','status'): ('/usr/local/sbin/ffn-phy','status'),
    ('phy','set'): ('/usr/local/sbin/ffn-phy','set'),
    ('bcm','status'): ('/usr/local/sbin/ffn-bcm-service','status'),
    ('bcm','set'): ('/usr/local/sbin/ffn-bcm-service','set'),
    ('faceplate', 'status'): ('/usr/local/sbin/ffn-faceplate', 'status'),
    ('faceplate', 'set'): ('/usr/local/sbin/ffn-faceplate', 'set'),
    ('dataplane', 'status'): ('/usr/local/sbin/ffn-dp-agent', 'status'),
    ('network', 'status'): ('/usr/local/sbin/ffn-network', 'status'),
    ('network', 'patch'): ('/usr/local/sbin/ffn-network', 'patch'),
    ('network', 'lookup'): ('/usr/local/sbin/ffn-network', 'lookup'),
    ('overlay', 'status'): ('/usr/local/sbin/ffn-overlay', 'status'),
    ('overlay', 'set'): ('/usr/local/sbin/ffn-overlay', 'set'),
    ('inspection', 'status'): ('/usr/local/sbin/ffn-inspection', 'status'),
    ('inspection', 'set'): ('/usr/local/sbin/ffn-inspection', 'set'),
    ('thermal', 'status'): ('/usr/local/sbin/ffn-thermal', 'status'),
    ('thermal', 'auto'): ('/usr/local/sbin/ffn-thermal', 'auto'),
    ('thermal', 'full'): ('/usr/local/sbin/ffn-thermal', 'full'),
    ('chassis', 'status'): ('/usr/local/sbin/ffn-cp', 'python3 /usr/local/sbin/ffn_chassis_led.py'),
    ('fabric', 'status'): ('/usr/bin/systemctl', 'is-active', 'ffn-fabric.service'),
}
LIMIT = 1024 * 1024


def legacy_router(current_user, require_admin, record_audit):
    api = APIRouter(prefix='/api/bcm')
    ctl = Controller()

    @api.post('/port/{port}/enable')
    async def enable(port: int, request: Request, user=Depends(current_user)):
        require_admin(user)
        data = await body(request)
        if set(data) != {'enable'} or type(data['enable']) is not bool:
            raise HTTPException(422, 'Expected boolean enable')
        observed = await ctl.run('faceplate','status')
        match = next((p for p in observed['ports'] if p['bcm_port']==port),None)
        if not match: raise HTTPException(422, 'Only mapped faceplate ports are controllable')
        await record_audit(user['username'],'faceplate_legacy_request','port=%d'%match['port'])
        result = await ctl.run('faceplate','set',{'revision':observed['revision'],'port':match['port'],'enabled':data['enable']})
        return dict(result,ok=True)

    @api.post('/port/{port}/loopback')
    async def loopback(port: int, user=Depends(current_user)):
        require_admin(user)
        raise HTTPException(503,'Loopback has no commissioned MP daemon adapter; direct ASIC bypass is disabled')

    return api


class Controller:
    async def run(self, resource, action, payload=None):
        if (resource, action) not in COMMANDS:
            raise HTTPException(404, 'Unknown appliance operation')
        from ffn_plane_api import rpc
        target = os.environ.get('FFN_PLANE_SOCKET', '/run/ffn-plane-mp/control.sock')
        data = dict(payload or {})
        operation = 'apply' if action in ('patch','set','auto','full') else action
        if resource == 'thermal' and operation == 'apply':
            data = {'revision':0, 'operation':action}
        request = {'v':1, 'id':str(uuid.uuid4()), 'resource':resource, 'action':operation, 'payload':data}
        try:
            response = await rpc(target, request)
        except (OSError, ValueError, asyncio.TimeoutError):
            raise HTTPException(503, 'MP control daemon unavailable or outcome unknown; request ID '+request['id'])
        if not response.get('ok'):
            raise HTTPException(409 if response.get('state') == 'rejected' else 502,
                'MP control '+response.get('state','unknown')+'; request ID '+request['id']+
                '. '+str(response.get('error','Refresh status')))
        return dict(response['result'], control={'id':request['id'], 'trace':response.get('trace'), 'state':response['state']})


async def body(request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 65536:
            raise HTTPException(413, 'Configuration exceeds 64 KiB')
    try:
        value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError):
        raise HTTPException(422, 'Expected a JSON object')


def runtime_router(current_user, require_admin, record_audit):
    return router(current_user, require_admin, record_audit, prefix='/api/system/runtime')


async def inspection_activation(ctl, result, attempts=10):
    """Persistence is not activation. Observe the DP's live revision separately."""
    accepted = result.get('accepted', {})
    expected = accepted.get('revision')
    if type(expected) is not int:
        return dict(result, activation='unknown')
    deadline = time.monotonic() + 8
    for attempt in range(attempts):
        try:
            observed = await asyncio.wait_for(ctl.run('inspection', 'status'),
                                             timeout=max(0.01, deadline - time.monotonic()))
        except (HTTPException, OSError, asyncio.TimeoutError):
            return dict(result, activation='unknown', expected_revision=expected)
        runtime = observed.get('runtime') or {}
        configured = (observed.get('config') or {}).get('revision')
        if configured != expected:
            return dict(result, activation='superseded', expected_revision=expected)
        if runtime.get('reload_error'):
            return dict(result, activation='failed', expected_revision=expected)
        if observed.get('running') and runtime.get('revision') == expected:
            return dict(result, activation='active', expected_revision=expected)
        if attempt + 1 < attempts:
            await asyncio.sleep(0.25)
    return dict(result, activation='pending', expected_revision=expected)


def router(current_user, require_admin, record_audit, controller=None, prefix='/api/pa5200'):
    api = APIRouter(prefix=prefix, tags=['PA-5220 controls'])
    ctl = controller or Controller()
    lock = asyncio.Lock()

    @api.get('/status')
    async def status(user=Depends(current_user)):
        async def one(resource):
            try:
                return resource, {'available': True, 'data': await ctl.run(resource, 'status')}
            except HTTPException as e:
                return resource, {'available': False, 'error': e.detail}
            except OSError:
                return resource, {'available': False, 'error': 'Controller unavailable'}
        resources = dict(await asyncio.gather(*(one(r) for r in (
            'network', 'overlay', 'inspection', 'thermal', 'chassis', 'fabric', 'dataplane'))))
        return {'collected_at': time.time(), 'resources': resources,
                'can_write': user.get('role') in ('admin', 'superuser'),
                'provider': 'pa5200', 'cpu_role': 'management',
                'capabilities': {'ports': ['p1', 'p3', 'p5', 'p13'], 'mtu': 1500,
                    'forwarding': 'software relay', 'hardware_flow_offload': False,
                    'dynamic_routing': 'FRR lab validated; production peer configuration not integrated',
                    'macsec_key_management': False,
                    'configuration': 'Immediate persistent controller changes; separate from XML candidate/commit'}}

    @api.get('/{resource}')
    async def get(resource: str, user=Depends(current_user)):
        return await ctl.run(resource, 'status')

    @api.post('/network/lookup')
    async def lookup(request: Request, user=Depends(current_user)):
        data = await body(request)
        if set(data) - {'dst', 'src', 'vrf'} or not isinstance(data.get('dst'), str):
            raise HTTPException(422, 'Route lookup requires dst and optional src/vrf')
        return await ctl.run('network', 'lookup', data)

    @api.post('/{resource}/{action}')
    async def change(resource: str, action: str, request: Request, user=Depends(current_user)):
        require_admin(user)
        if (resource, action) not in {('network', 'patch'), ('overlay', 'set'),
                ('inspection', 'set'), ('bcm', 'set'), ('phy', 'set'), ('faceplate', 'set'), ('thermal', 'auto'), ('thermal', 'full')}:
            raise HTTPException(404, 'Unknown appliance operation')
        data = await body(request)
        allowed = {'phy': {'revision','phy','speed'}, 'bcm': {'revision','operation','acknowledge_link_outage'}, 'faceplate': {'revision','port','enabled','speed'}, 'network': {'revision', 'ports', 'routes', 'vrfs', 'rules'},
                   'overlay': {'revision', 'links'},
                   'inspection': {'revision', 'mode', 'ports', 'literal', 'detectors'}, 'thermal': set()}[resource]
        if set(data) - allowed:
            raise HTTPException(422, 'Unknown configuration fields')
        if resource != 'thermal' and (type(data.get('revision')) is not int or data['revision'] < 0):
            raise HTTPException(422, 'A current integer configuration revision is required')
        async with lock:
            detail = '%s/%s revision=%s' % (resource, action, data.get('revision', 'n/a'))
            await record_audit(user['username'], 'pa5200_request', detail)
            try:
                result = await ctl.run(resource, action, data if resource != 'thermal' else None)
                if resource == 'inspection':
                    result = await inspection_activation(ctl, result)
            except HTTPException as e:
                await record_audit(user['username'], 'pa5200_failed', detail + ' http=%d' % e.status_code)
                raise
            if resource == 'inspection':
                detail += ' activation=' + result['activation']
            await record_audit(user['username'], 'pa5200_completed', detail)
            return result

    return api
