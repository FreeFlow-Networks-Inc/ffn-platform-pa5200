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

from fastapi import APIRouter, Depends, HTTPException, Request

COMMANDS = {
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


class Controller:
    async def run(self, resource, action, payload=None):
        argv = COMMANDS.get((resource, action))
        if argv is None:
            raise HTTPException(404, 'Unknown appliance operation')
        if not os.access(argv[0], os.X_OK):
            raise HTTPException(503, 'Appliance controller is not installed')
        data = json.dumps(payload, allow_nan=False).encode() if payload is not None else b''
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True)
        except OSError:
            raise HTTPException(503, 'Appliance controller could not be started')

        async def read(stream):
            parts, size = [], 0
            while True:
                part = await stream.read(65536)
                if not part:
                    return b''.join(parts)
                size += len(part)
                if size > LIMIT:
                    raise HTTPException(502, 'Controller response exceeds limit; refresh status')
                parts.append(part)

        async def exchange():
            proc.stdin.write(data)
            await proc.stdin.drain()
            proc.stdin.close()
            out, err = await asyncio.gather(read(proc.stdout), read(proc.stderr))
            await proc.wait()
            return out, err

        try:
            out, err = await asyncio.wait_for(exchange(), timeout=90 if payload is not None else 25)
        except asyncio.TimeoutError:
            raise HTTPException(504, 'Controller timed out; outcome may be unknown. Refresh status before retrying.')
        finally:
            if proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
        if resource == 'fabric':
            state = out.decode(errors='replace').strip()
            if proc.returncode in (0, 3, 4) and state in ('active', 'inactive', 'failed', 'unknown', 'activating', 'deactivating'):
                return {'state': state, 'running': state == 'active'}
        if proc.returncode:
            # Never return traceback, SSH diagnostics or configuration secrets.
            if b'revision conflict' in err.lower():
                raise HTTPException(409, 'Configuration changed; reload before applying')
            if b'ValueError:' in err:
                raise HTTPException(422, 'Controller rejected configuration; check fields and port dependencies')
            raise HTTPException(502, 'Controller failed; refresh status before retrying')
        if resource == 'thermal' and action != 'status':
            return {'requested_mode': action, 'completed': True}
        try:
            result = json.loads(out)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, UnicodeError):
            raise HTTPException(502, 'Controller returned an invalid response')


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


def router(current_user, require_admin, record_audit, controller=None):
    api = APIRouter(prefix='/api/pa5200', tags=['PA-5220 controls'])
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
            'network', 'overlay', 'inspection', 'thermal', 'chassis', 'fabric'))))
        return {'collected_at': time.time(), 'resources': resources,
                'can_write': user.get('role') in ('admin', 'superuser'),
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
                ('inspection', 'set'), ('thermal', 'auto'), ('thermal', 'full')}:
            raise HTTPException(404, 'Unknown appliance operation')
        data = await body(request)
        allowed = {'network': {'revision', 'ports', 'routes', 'vrfs', 'rules'},
                   'overlay': {'revision', 'links'},
                   'inspection': {'revision', 'mode', 'ports', 'literal'}, 'thermal': set()}[resource]
        if set(data) - allowed:
            raise HTTPException(422, 'Unknown configuration fields')
        if resource != 'thermal' and (type(data.get('revision')) is not int or data['revision'] < 0):
            raise HTTPException(422, 'A current integer configuration revision is required')
        async with lock:
            detail = '%s/%s revision=%s' % (resource, action, data.get('revision', 'n/a'))
            await record_audit(user['username'], 'pa5200_request', detail)
            try:
                result = await ctl.run(resource, action, data if resource != 'thermal' else None)
            except HTTPException as e:
                await record_audit(user['username'], 'pa5200_failed', detail + ' http=%d' % e.status_code)
                raise
            await record_audit(user['username'], 'pa5200_completed', detail)
            return result

    return api
