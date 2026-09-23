#!/usr/bin/env python3
"""Publish one built CP or DP artifact in the existing signed patch catalog."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'management'))


def publish(directory, build, role, version, seed_path, public_path, notes=''):
    from plane_images import metadata, inspect_image, sha, role_name
    from ffn_patch import lock, read, save, atomic
    from ffn_payload import load_hex, sign_manifest, verify_manifest, ffn_ed25519
    role_name(role)
    directory, build, seed_path = Path(directory), Path(build), Path(seed_path)
    if seed_path.resolve().is_relative_to(directory.resolve()) or seed_path.stat().st_mode & 0o077:
        raise ValueError('Keep signing seed private and outside publication directory')
    seed, public = load_hex(str(seed_path)), load_hex(str(public_path))
    if not seed or len(seed) != 32 or ffn_ed25519.publickey(seed) != public:
        raise ValueError('Signing key mismatch')
    manifest = json.loads((build / 'manifest.json').read_text())
    assets = [a for a in manifest['assets'] if a['role'] == role]
    if manifest.get('platform') != 'pa5200' or len(assets) != 1:
        raise ValueError('Expected one PA-5200 role in build manifest')
    asset = assets[0]
    if asset['name'] != 'ffn-pa5200-' + role + '.tar.xz':
        raise ValueError('Unexpected build asset name')
    archive = build / asset['name']
    if archive.is_symlink() or not archive.is_file() or sha(archive) != asset['sha256'] or archive.stat().st_size != asset['size']:
        raise ValueError('Built artifact hash/size mismatch')
    item = {k: manifest[k] for k in ('platform', 'architecture', 'runtime_abi', 'core_commit', 'platform_commit')}
    item.update(role=role, version=version, kernel_release=asset['kernel_release'], size=asset['size'],
                sha256=asset['sha256'], file='pa5200-' + role + '-' + asset['sha256'] + '.tar.xz',
                notes=notes, hardware_boot_verified=manifest.get('hardware_boot_verified') is True)
    inspect_image(archive, item)
    with lock(directory):
        current = read(directory / 'manifest.json', {})
        if current:
            ok, why = verify_manifest(current, pub=public)
            if not ok:
                raise ValueError(why)
        previous = current.get('payloads', {}).get('pa5200-' + role, {})
        if previous.get('version') == version and previous.get('sha256') != item['sha256']:
            raise ValueError('Cannot reuse a plane image version for different bytes')
        item['published'] = max(int(time.time()), previous.get('published', 0) + 1)
        catalog = dict(current)
        catalog['payloads'] = dict(current.get('payloads', {}), **{'pa5200-' + role: item})
        catalog['updated'] = max(item['published'], current.get('updated', 0))
        catalog['signature'], catalog['sig_alg'] = sign_manifest(catalog, seed=seed)
        metadata(catalog, role, public)
        target = directory / item['file']
        if target.exists() or target.is_symlink():
            if target.is_symlink() or sha(target) != item['sha256']:
                raise ValueError('Existing immutable image differs')
        else:
            fd, name = tempfile.mkstemp(prefix='.image-', dir=directory)
            try:
                with os.fdopen(fd, 'wb') as out, archive.open('rb') as src:
                    shutil.copyfileobj(src, out, 1024 * 1024)
                    out.flush(); os.fsync(out.fileno())
                if sha(name) != item['sha256']:
                    raise ValueError('Image changed while publishing')
                os.chmod(name, 0o644)
                os.replace(name, target)
            finally:
                if os.path.exists(name): os.unlink(name)
        if current:
            history = directory / '.history'
            raw = json.dumps(current, indent=2).encode()
            import hashlib
            atomic(history / (hashlib.sha256(raw).hexdigest() + '.json'), raw)
        atomic(directory / 'manifest.json', json.dumps(catalog, indent=2).encode(), mode=0o644)
    return item


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--core', required=True, type=Path)
    p.add_argument('--dir', required=True, type=Path)
    p.add_argument('--build', required=True, type=Path)
    p.add_argument('--role', choices=('cp', 'dp'), required=True)
    p.add_argument('--version', required=True)
    p.add_argument('--seed', required=True, type=Path)
    p.add_argument('--public-key', required=True, type=Path)
    p.add_argument('--notes', default='')
    a = p.parse_args()
    sys.path.insert(0, str(a.core / 'opt'))
    print(json.dumps(publish(a.dir, a.build, a.role, a.version, a.seed, a.public_key, a.notes), indent=2))
