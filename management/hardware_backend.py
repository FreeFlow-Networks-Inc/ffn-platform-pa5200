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
