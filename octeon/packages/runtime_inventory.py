#!/usr/bin/env python3
"""Report CP/DP runtime package gaps without installing or executing target code."""
import argparse
import json
from pathlib import Path


def paragraphs(text):
    result, row, key = [], {}, None
    for line in text.splitlines() + ['']:
        if not line:
            if row:
                result.append(row)
            row, key = {}, None
        elif line[0].isspace():
            if key is None:
                raise ValueError('Orphan package metadata continuation')
            row[key] += '\n' + line
        else:
            key, sep, value = line.partition(':')
            if not sep or key in row:
                raise ValueError('Malformed or duplicate package field')
            row[key] = value.strip()
    return result


def report(role, indexes=(), root=None):
    policy = json.loads(Path(__file__).with_name('runtime-requirements.json').read_text())
    if role not in ('cp', 'dp'):
        raise ValueError('Expected cp or dp')
    architecture = policy['architecture']
    available, installed = {}, {}
    for index in indexes:
        for package in paragraphs(index.read_text()):
            if package.get('Architecture') in (architecture, 'all') and package.get('Version'):
                available.setdefault(package['Package'], set()).add(package['Version'])
    if root is not None:
        root = root.resolve(strict=True)
        status = (root / 'var/lib/dpkg/status').resolve(strict=True)
        if not status.is_relative_to(root):
            raise ValueError('Package database escapes image root')
        for package in paragraphs(status.read_text()):
            if package.get('Status') != 'install ok installed':
                continue
            if package.get('Architecture') not in (architecture, 'all'):
                raise ValueError('Foreign architecture in installed root: ' + package.get('Package', '?'))
            installed[package['Package']] = package['Version']
    required = policy['common'] + policy[role]
    return {
        'schema': 1, 'role': role, 'architecture': architecture,
        'packages': [{'name': name, 'installed_version': installed.get(name),
                      'available_versions': sorted(available.get(name, []))} for name in required],
        'missing_from_root': [n for n in required if n not in installed] if root is not None else None,
        'missing_from_repository': [n for n in required if n not in available] if indexes else None,
        'hardware_qualification': 'not-assessed',
        'note': 'Package presence alone does not prove dependency closure, native hardware integration or boot readiness.'
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', choices=('cp', 'dp'), required=True)
    parser.add_argument('--index', type=Path, action='append', default=[])
    parser.add_argument('--rootfs', type=Path)
    args = parser.parse_args()
    if not args.index and args.rootfs is None:
        parser.error('Supply --index, --rootfs or both')
    result = report(args.role, args.index, args.rootfs)
    print(json.dumps(result, indent=2))
    raise SystemExit(1 if result['missing_from_root'] or result['missing_from_repository'] else 0)
