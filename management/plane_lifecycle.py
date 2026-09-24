#!/usr/bin/env python3
"""MP-owned plane restarts. Boot owners are locally commissioned, never UI input."""
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

sys.path.insert(0, '/opt/ffn-ngfw-v2')
from ffn_patch import read, save, lock
from ffn_control_plane import control_rpc

ROOT = Path('/var/lib/ffn-ngfw/pa5200-restarts')
CONFIG = Path('/etc/ffn-ngfw/pa5200-restarts.json')
ROLES = ('cp', 'dp')
UNITS = {role: 'ffn-pa5200-restart-' + role + '.service' for role in ROLES}


def observations():
    state = asyncio.run(control_rpc('state/control', timeout=5))
    result = {}
    for role in ROLES:
        agents = [a for a in state.get('agents', {}).values() if a.get('role') == role
                  and (a.get('last_observation') or {}).get('platform') == 'pa5200']
        a = agents[0] if len(agents) == 1 else {}
        report = (a.get('last_observation') or {}).get('report') or {}
        boot = report.get('boot_id')
        try:
            if str(uuid.UUID(boot)) != boot: boot = None
        except (ValueError, TypeError, AttributeError):
            boot = None
        result[role] = {'boot_id': boot, 'fresh': a.get('fresh') is True and bool(boot),
                        'ready': a.get('ready') is True, 'age_seconds': a.get('age_seconds'),
                        'restart_acknowledged': report.get('restart_acknowledged') is True,
                        'runtime': report.get('runtime')}
    return result


class Lifecycle:
    def __init__(self, root=ROOT, config=CONFIG, observe=observations):
        self.root, self.config, self.observe = Path(root), Path(config), observe

    def settings(self):
        if not self.config.exists(): return {}
        stat = self.config.stat()
        if self.config.is_symlink() or stat.st_uid != 0 or stat.st_mode & 0o022 or stat.st_size > 8192:
            raise ValueError('Restart commissioning file must be root-owned and protected')
        data = json.loads(self.config.read_text())
        if set(data) != {'schema', 'roles'} or data['schema'] != 1 or not isinstance(data['roles'], dict):
            raise ValueError('Invalid restart commissioning file')
        for role, item in data['roles'].items():
            if role not in ROLES or set(item) != {'enabled', 'timeout'} or type(item['enabled']) is not bool \
                    or type(item['timeout']) is not int or not 30 <= item['timeout'] <= 1200:
                raise ValueError('Invalid restart role configuration')
        return data['roles']

    def unit(self, name):
        p = subprocess.run(['systemctl', 'show', name, '--property=LoadState,ActiveState,Type,RemainAfterExit,Result'],
                           capture_output=True, text=True, timeout=10, check=True)
        return dict(line.split('=', 1) for line in p.stdout.splitlines() if '=' in line)

    def alive(self, job):
        return self.unit('ffn-plane-restart-' + job['id'] + '.service').get('ActiveState') in ('active', 'activating')

    def status(self):
        state = read(self.root / 'state.json', {'revision': 0})
        try: settings, error = self.settings(), None
        except (ValueError, OSError) as exc: settings, error = {}, str(exc)
        try: observed = self.observe()
        except Exception: observed = {r: {'fresh': False, 'ready': False, 'boot_id': None} for r in ROLES}
        job = state.get('job') or {}
        busy = job.get('status') in ('queued', 'running')
        if busy and time.time() - job['created_at'] > 30 and not self.alive(job):
            job = dict(job, status='interrupted', message='Restart worker stopped; inspect processor and boot owner state')
            busy = False
        units = {r: self.unit(UNITS[r]) for r in ROLES}
        busy = busy or any(u.get('ActiveState') in ('active', 'activating', 'deactivating') for u in units.values())
        roles = {}
        for role in ROLES:
            u = units[role]
            reason = error or (None if settings.get(role, {}).get('enabled') else 'Restart owner is not commissioned')
            if not reason and (u.get('LoadState') != 'loaded' or u.get('Type') != 'oneshot' or u.get('RemainAfterExit') != 'no'):
                reason = 'A dedicated oneshot restart service is required'
            if not reason and not observed[role]['fresh']: reason = 'Fresh processor boot identity is unavailable'
            if not reason and role == 'dp' and not (observed['cp']['fresh'] and observed['cp']['ready']):
                reason = 'Control Plane must be connected and ready to restart Data Plane'
            roles[role] = dict(observed[role], restart_available=not reason and not busy,
                               reason=reason or ('Another processor restart is in progress' if busy else None))
        return {'platform': 'pa5200', 'config': {'revision': state['revision']}, 'roles': roles,
                'job': job, 'busy': busy, 'image_activation_supported': False}

    def validate(self, payload):
        if not isinstance(payload, dict) or set(payload) != {'role', 'operation', 'revision', 'expected_boot_id', 'acknowledge_outage'}:
            raise ValueError('Expected role, restart operation, revision, boot identity and outage acknowledgment')
        role = payload['role']
        if role not in ROLES or payload['operation'] != 'restart' or payload['acknowledge_outage'] is not True \
                or type(payload['revision']) is not int:
            raise ValueError('Explicit processor restart and outage acknowledgment required')
        state = self.status()
        if payload['revision'] != state['config']['revision']: raise ValueError('Restart state changed; refresh')
        target = state['roles'][role]
        if not target['restart_available']: raise ValueError(target['reason'])
        if payload['expected_boot_id'] != target['boot_id']: raise ValueError('Processor boot identity changed; refresh')
        return state

    def launch(self, job):
        subprocess.run(['systemd-run', '--quiet', '--unit=ffn-plane-restart-' + job['id'],
                        '--property=RuntimeMaxSec=2500', '--property=UMask=0077',
                        sys.executable, str(Path(__file__).resolve()), 'worker', job['id']],
                       capture_output=True, timeout=15, check=True)

    def submit(self, payload):
        with lock(self.root):
            state = self.validate(payload)
            job = {'id': uuid.uuid4().hex, 'role': payload['role'], 'status': 'queued',
                   'created_at': time.time(), 'before': state['roles'], 'message': 'Restart queued'}
            saved = {'revision': state['config']['revision'] + 1, 'job': job}
            save(self.root / 'state.json', saved)
            try: self.launch(job)
            except Exception:
                # Dispatch may have succeeded before a timeout. Never issue a duplicate reset.
                job.update(status='queued', message='Dispatch uncertain; inspect restart worker before retrying')
                save(self.root / 'state.json', saved)
            return job

    def start_owner(self, role, timeout):
        subprocess.run(['systemctl', 'start', UNITS[role]], check=True, timeout=timeout, capture_output=True)
        if self.unit(UNITS[role]).get('Result') != 'success': raise ValueError('Restart owner did not succeed')

    def work(self, ident, clock=time.monotonic, sleep=time.sleep):
        if not re.fullmatch('[0-9a-f]{32}', ident): raise ValueError('Invalid worker identity')
        with lock(self.root):
            state = read(self.root / 'state.json', {})
            job = state.get('job') or {}
            if job.get('id') != ident or job.get('status') != 'queued': raise ValueError('No matching queued restart')
            role = job['role']; other = 'dp' if role == 'cp' else 'cp'
            try:
                settings = self.settings()[role]
                before = self.observe()
                if not settings['enabled'] or not before[role]['fresh'] or before[role]['boot_id'] != job['before'][role]['boot_id']:
                    raise ValueError('Restart preconditions changed; no reset dispatched')
                if role == 'dp' and not (before['cp']['fresh'] and before['cp']['ready']):
                    raise ValueError('Control Plane is no longer ready; no reset dispatched')
                units = {r: self.unit(UNITS[r]) for r in ROLES}
                owner = units[role]
                if owner.get('LoadState') != 'loaded' or owner.get('Type') != 'oneshot' or owner.get('RemainAfterExit') != 'no' \
                        or any(u.get('ActiveState') in ('active', 'activating', 'deactivating') for u in units.values()):
                    raise ValueError('Boot owner changed or is already running; no reset dispatched')
                job.update(status='running', message='Restarting ' + role.upper(), started_at=time.time())
                save(self.root / 'state.json', state)
                self.start_owner(role, settings['timeout'])
                job['message'] = 'Waiting for a new boot identity and ready agent'
                save(self.root / 'state.json', state)
                deadline = clock() + settings['timeout']
                while clock() < deadline:
                    try: after = self.observe()
                    except Exception: sleep(3); continue
                    target = after[role]
                    if target['fresh'] and (target['ready'] or target.get('restart_acknowledged')) and target['boot_id'] != before[role]['boot_id']:
                        job['after'] = after
                        if after[other]['fresh'] and before[other]['boot_id'] and after[other]['boot_id'] != before[other]['boot_id']:
                            raise ValueError('Unexpected reboot of the other processor; inspect boot owner')
                        message = (role.upper() + ' restarted; new boot and ready agent verified. Traffic recovery must be checked separately.'
                                   if target['ready'] else role.upper() + ' restarted into recovery runtime; control agent acknowledged the new boot. Dataplane is not ready for forwarding.')
                        job.update(status='succeeded', ready=target['ready'], message=message)
                        break
                    sleep(3)
                else: raise ValueError('Restart not verified: no new ready processor boot before deadline')
            except Exception as exc:
                job.update(status='failed', message=str(exc)[:512] or 'Restart outcome unverified; inspect boot owner')
            finally:
                job['finished_at'] = time.time(); state['revision'] += 1
                save(self.root / 'state.json', state)
                history = read(self.root / 'history.json', [])
                save(self.root / 'history.json', ([job] + history)[:50])
            return job


def main():
    try:
        client = Lifecycle(); action = sys.argv[1]
        if action == 'worker': result = client.work(sys.argv[2])
        else:
            payload = json.load(sys.stdin)
            if action == 'status':
                if payload: raise ValueError('Status takes no payload')
                result = client.status()
            elif action == 'validate': client.validate(payload); result = {'validated': True}
            elif action == 'apply': result = client.submit(payload)
            else: raise ValueError('Unknown lifecycle operation')
        print(json.dumps(result)); return 0
    except Exception as exc:
        print(json.dumps({'error': str(exc)[:512]})); return 2


if __name__ == '__main__': raise SystemExit(main())
