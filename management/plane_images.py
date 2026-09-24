#!/usr/bin/env python3
"""MP-owned, independently signed CP/DP image downloads. Never resets a plane.

The platform registers this adapter with controld. WebUI and console requests
only select a role and a previously verified digest, never a path or command.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import uuid

sys.path.insert(0, '/opt/ffn-ngfw-v2')
from ffn_patch import atomic, save, read, lock, server_url, HTTPSOnly, download
from ffn_payload import verify_manifest, load_hex

ROOT = Path('/var/lib/ffn-ngfw/pa5200-images')
SERVER = Path('/etc/ffn-ngfw/update-server.conf')
PUBLIC = Path('/etc/ffn-ngfw/update.pub')
ROLES = ('cp', 'dp')
MAX_IMAGE = 2 * 1024**3
MAX_CATALOG = 1024 * 1024


def role_name(role):
    if role not in ROLES:
        raise ValueError('Select cp or dp')
    return role


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def metadata(catalog, role, public):
    role_name(role)
    if catalog.get('sig_alg') != 'ed25519':
        raise ValueError('Ed25519 signed image catalog required')
    ok, why = verify_manifest(catalog, pub=public)
    if not ok:
        raise ValueError(why)
    item = catalog.get('payloads', {}).get('pa5200-' + role)
    if item is None:
        return None
    if not isinstance(item, dict):
        raise ValueError('Invalid plane image metadata')
    expected = {'platform': 'pa5200', 'role': role, 'architecture': 'mips64eb', 'runtime_abi': 1}
    if any(item.get(k) != v or type(item.get(k)) != type(v) for k, v in expected.items()):
        raise ValueError('Image platform, role, architecture or runtime ABI mismatch')
    for field, pattern in [('sha256', '[0-9a-f]{64}'), ('file', r'pa5200-' + role + r'-[0-9a-f]{64}\.tar\.xz'),
                           ('version', r'[A-Za-z0-9][A-Za-z0-9.+_-]{0,99}'),
                           ('kernel_release', r'[A-Za-z0-9.+_-]{1,128}'),
                           ('core_commit', '[0-9a-f]{40}'), ('platform_commit', '[0-9a-f]{40}')]:
        if not isinstance(item.get(field), str) or not re.fullmatch(pattern, item[field]):
            raise ValueError('Invalid image ' + field)
    if item['file'] != 'pa5200-' + role + '-' + item['sha256'] + '.tar.xz':
        raise ValueError('Image filename must include its digest')
    if type(item.get('size')) is not int or not 0 < item['size'] <= MAX_IMAGE:
        raise ValueError('Invalid image size')
    if type(item.get('published')) is not int or item['published'] <= 0:
        raise ValueError('Invalid publication sequence')
    if type(item.get('hardware_boot_verified')) is not bool:
        raise ValueError('Image qualification must be explicit')
    return item


def inspect_image(path, item):
    """Read only the bounded top-level descriptor; archives are never extracted."""
    with tarfile.open(path, 'r:xz') as tar:
        # Builders put image.json first. Refuse archives hiding it behind an
        # arbitrarily large rootfs; a signed payload still has resource limits.
        member = tar.next()
        if not member or member.name != 'image.json' or not member.isfile() or not 0 < member.size <= 65536:
            raise ValueError('Image must start with a bounded regular image.json')
        detail = json.load(tar.extractfile(member))
    for field in ('role', 'architecture', 'runtime_abi', 'core_commit', 'platform_commit', 'kernel_release', 'hardware_boot_verified'):
        if detail.get(field) != item[field] or type(detail.get(field)) != type(item[field]):
            raise ValueError('Image descriptor mismatch: ' + field)
    if (detail.get('operating_system') or {}).get('distribution') != 'debian':
        raise ValueError('OCTEON images must contain native Debian userspace')
    return detail


def stream_image(url, target, size):
    server_url(url)
    opener = urllib.request.build_opener(HTTPSOnly(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    start, count = time.monotonic(), 0
    with opener.open(url, timeout=30) as response, target.open('xb') as out:
        server_url(response.url)
        while True:
            if time.monotonic() - start > 1800:
                raise ValueError('Image download deadline exceeded')
            block = response.read(min(1024 * 1024, size + 1 - count))
            if not block:
                break
            count += len(block)
            if count > size:
                raise ValueError('Image exceeds signed size')
            out.write(block)
        out.flush()
        os.fsync(out.fileno())
    if count != size:
        raise ValueError('Truncated image download')


class ImageClient:
    def __init__(self, root=ROOT, server=SERVER, public=PUBLIC):
        self.root, self.server, self.public = Path(root), Path(server), Path(public)

    def url(self):
        values = [line.strip()[4:].strip() for line in self.server.read_text().splitlines()
                  if line.strip().startswith('url=')]
        if len(values) != 1:
            raise ValueError('Configure one update server in Patch Management')
        return server_url(values[0])

    def state(self, role):
        return read(self.root / role_name(role) / 'state.json', {'revision': 0})

    def status(self):
        states = {role: self.state(role) for role in ROLES}
        for role, state in states.items():
            job = state.get('job') or {}
            if job.get('status') in ('queued', 'running') and time.time() - job['created_at'] > 60 and not self.alive(job):
                state['job'] = dict(job, status='interrupted', message='Download worker stopped; retry is available')
        try:
            url, error = self.url(), None
        except (OSError, ValueError) as exc:
            url, error = '', str(exc)
        return {'platform': 'pa5200', 'roles': states, 'config': {'revision': sum(s['revision'] for s in states.values())},
                'server': url, 'configuration_error': error,
                'public_key_present': self.public.is_file(), 'activation_supported': False,
                'activation_note': 'Downloads are staged on the MP. Boot selection, restart and live agent validation are separate operations.'}

    def alive(self, job):
        result = subprocess.run(['systemctl', 'is-active', '--quiet', 'ffn-plane-image-' + job['id'] + '.service'],
                                timeout=10, capture_output=True)
        return result.returncode == 0

    def validate(self, request):
        if not isinstance(request, dict) or set(request) != {'role', 'operation', 'revision', 'sha256'}:
            raise ValueError('Expected role, operation, revision and sha256')
        role, action = role_name(request['role']), request['operation']
        if action not in ('check', 'download'):
            raise ValueError('Image operation must be check or download')
        if type(request['revision']) is not int or request['revision'] < 0:
            raise ValueError('Invalid revision')
        if action == 'check' and request['sha256'] != '':
            raise ValueError('Check takes no digest')
        self.url()
        public = load_hex(str(self.public))
        if not public or len(public) != 32:
            raise ValueError('Provision the update server public key first')
        state = self.state(role)
        if state['revision'] != request['revision']:
            raise ValueError('Image state changed; refresh before retrying')
        old = state.get('job') or {}
        if old.get('status') in ('queued', 'running') and (time.time() - old['created_at'] <= 60 or self.alive(old)):
            raise ValueError('This plane already has an image operation running')
        if action == 'download':
            available = state.get('available') or {}
            if not available or request['sha256'] != available['sha256']:
                raise ValueError('Select the checked image digest')
        return {'valid': True, 'role': role, 'operation': action, 'revision': state['revision']}

    def submit(self, request):
        self.validate(request)
        role, action = request['role'], request['operation']
        directory = self.root / role
        with lock(directory):
            self.validate(request)
            state = self.state(role)
            job = dict(id=uuid.uuid4().hex, role=role, operation=action, sha256=request['sha256'],
                       created_at=time.time(), status='queued', server=self.url())
            state.update(revision=state['revision'] + 1, job=job)
            save(directory / 'state.json', state)
            try:
                self.launch(job)
            except Exception:
                job.update(status='failed', message='Could not start image worker', finished_at=time.time())
                save(directory / 'state.json', state)
            return job

    def launch(self, job):
        subprocess.run(['systemd-run', '--quiet', '--unit=ffn-plane-image-' + job['id'],
                        '--property=RuntimeMaxSec=1900', '--property=UMask=0077',
                        sys.executable, str(Path(__file__).resolve()), 'worker', job['role'], job['id']],
                       check=True, timeout=15, capture_output=True)

    def work(self, role, ident, fetch=download, stream=stream_image):
        role_name(role)
        if not isinstance(ident, str) or not re.fullmatch('[0-9a-f]{32}', ident):
            raise ValueError('Invalid image job identity')
        directory = self.root / role
        with lock(directory):
            state = self.state(role)
            job = state.get('job') or {}
            if job.get('id') != ident or job.get('status') != 'queued':
                raise ValueError('No matching queued worker')
            job.update(status='running', started_at=time.time())
            save(directory / 'state.json', state)
            try:
                public = load_hex(str(self.public))
                if job['operation'] == 'check':
                    catalog = json.loads(fetch(job['server'] + '/manifest.json', MAX_CATALOG))
                    item = metadata(catalog, role, public)
                    prior = state.get('high_water') or {}
                    if item and (item['published'] < prior.get('published', 0) or
                                 (item['published'] == prior.get('published') and item['sha256'] != prior.get('sha256'))):
                        raise ValueError('Image catalog rollback or publication collision rejected')
                    if item:
                        state['high_water'] = {'published': item['published'], 'sha256': item['sha256']}
                    save(directory / 'catalog.json', catalog)
                    state.update(available=item, checked_at=time.time(), checked_server=job['server'])
                else:
                    if state.get('checked_server') != job['server']:
                        raise ValueError('Update server changed; check this plane again')
                    catalog = read(directory / 'catalog.json')
                    item = metadata(catalog, role, public)
                    if item is None or item['sha256'] != job['sha256']:
                        raise ValueError('Selected image no longer matches the verified catalog')
                    target = directory / 'cache' / item['sha256']
                    if target.is_symlink():
                        raise ValueError('Symlink image cache rejected')
                    if not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        if shutil.disk_usage(target.parent).free < item['size'] + 64 * 1024**2:
                            raise ValueError('Insufficient MP space for this plane image')
                        work = Path(tempfile.mkdtemp(prefix='.download-', dir=target.parent))
                        try:
                            archive = work / item['file']
                            stream(job['server'] + '/' + item['file'], archive, item['size'])
                            self.verify(archive, item)
                            save(work / 'catalog.json', catalog)
                            work.rename(target)
                        finally:
                            if work.exists():
                                shutil.rmtree(work)
                    else:
                        self.verify(target / item['file'], item)
                    state['staged'] = dict(item, path=str(target / item['file']), verified_at=time.time(), activated=False)
                job.update(status='succeeded', message='Catalog checked' if job['operation'] == 'check' else 'Image verified and staged; no plane restarted')
            except Exception as error:
                job.update(status='failed', message=str(error)[:512])
            finally:
                job['finished_at'] = time.time()
                state['revision'] += 1
                save(directory / 'state.json', state)
        return job

    @staticmethod
    def verify(path, item):
        if path.is_symlink() or not path.is_file() or path.stat().st_size != item['size'] or sha(path) != item['sha256']:
            raise ValueError('Plane image hash or size mismatch')
        inspect_image(path, item)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('status', 'validate', 'apply', 'worker'))
    p.add_argument('role', nargs='?', choices=ROLES)
    p.add_argument('job', nargs='?')
    args = p.parse_args()
    try:
        client = ImageClient()
        if args.action == 'worker':
            result = client.work(args.role, args.job)
        else:
            request = json.load(sys.stdin)
            if args.action == 'status' and request:
                raise ValueError('Status takes no payload')
            result = {'status': client.status, 'validate': lambda: client.validate(request),
                      'apply': lambda: client.submit(request)}[args.action]()
        print(json.dumps(result))
        # A recorded failed job is a known outcome, not an interrupted RPC.
        return 1 if args.action == 'worker' and result.get('status') == 'failed' else 0
    except Exception as error:
        print(json.dumps({'error': str(error)[:512]}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
