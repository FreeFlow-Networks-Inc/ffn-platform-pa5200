#!/usr/bin/env python3
"""Produce a reviewable image lock from an attested, qualified release manifest.

This does not commit a lock, publish a draft, install images or reset hardware.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', required=True, type=Path)
    p.add_argument('--repository', default='FreeFlow-Networks-Inc/ffn-platform-pa5200')
    p.add_argument('--tag', required=True)
    p.add_argument('--qualification', required=True, type=Path,
                   help='Lab report for this manifest, including CP and DP boot/agent checks')
    p.add_argument('--out', required=True, type=Path)
    a = p.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+', a.repository):
        raise SystemExit('Invalid repository')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', a.tag):
        raise SystemExit('Invalid release tag')
    manifest = json.loads(a.manifest.read_text())
    digest = hashlib.sha256(a.manifest.read_bytes()).hexdigest()
    report = json.loads(a.qualification.read_text())
    if (report.get('manifest_sha256') != digest or
            any(report.get(role, {}).get(check) is not True for role in ('cp', 'dp')
                for check in ('boot_verified', 'agent_handshake_verified'))):
        raise SystemExit('Matching successful CP/DP hardware qualification required')
    subprocess.run(['gh', 'attestation', 'verify', str(a.manifest), '--repo', a.repository,
                    '--signer-workflow', a.repository + '/.github/workflows/octeon-images.yml'],
                   check=True, timeout=120)
    release = json.loads(subprocess.check_output(['gh', 'release', 'view', a.tag,
                         '--repo', a.repository, '--json', 'isDraft,isPrerelease'], text=True))
    if release['isDraft'] or release['isPrerelease']:
        raise SystemExit('Publish the qualified release before promoting its image lock')
    # Verify that the chosen tag actually serves this attested manifest.
    import tempfile
    with tempfile.TemporaryDirectory() as temp:
        subprocess.run(['gh', 'release', 'download', a.tag, '--repo', a.repository,
                        '--pattern', 'manifest.json', '--dir', temp], check=True, timeout=120)
        if hashlib.sha256((Path(temp) / 'manifest.json').read_bytes()).hexdigest() != digest:
            raise SystemExit('Release tag serves a different manifest')
    lock = {key: manifest[key] for key in ('platform', 'architecture', 'runtime_abi',
                                          'platform_commit', 'core_commit')}
    lock.update(schema=1, enabled=True, repository=a.repository, tag=a.tag,
                manifest_sha256=digest,
                qualification_sha256=hashlib.sha256(a.qualification.read_bytes()).hexdigest())
    with a.out.open('x') as f:
        f.write(json.dumps(lock, indent=2) + '\n')


if __name__ == '__main__':
    main()
