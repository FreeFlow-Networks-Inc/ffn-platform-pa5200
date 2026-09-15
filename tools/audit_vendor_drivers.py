#!/usr/bin/env python3
"""Read-only ELF/module inventory. Keep output with the private VM references."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def command(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE)


def audit(root):
    modules = []
    for p in sorted(root.rglob('*.ko')):
        header = p.read_bytes()[:20]
        if header[:4] != b'\x7fELF' or header[4] != 2 or header[5] not in (1, 2):
            raise ValueError('expected ELF64 module: ' + str(p))
        machine = int.from_bytes(header[18:20], 'little' if header[5] == 1 else 'big')
        modinfo = command('readelf', '-p', '.modinfo', str(p))
        info = {}
        for line in modinfo.splitlines():
            if ']' in line:
                item = line.split(']', 1)[1].strip()
                if '=' in item:
                    key, value = item.split('=', 1)
                    info.setdefault(key, []).append(value)
        imports = sorted({line.split()[-1] for line in command('nm', '-u', str(p)).splitlines() if line.split()})
        private = [s for s in imports if s.startswith(('ksysd_', 'ixgbe_get_', 'ixgbe_set_',
                   'get_pan_', 'panic_set_', 'panic_reset_', 'cvmx_', '__cvmx_'))]
        modules.append({'path': str(p.relative_to(root)), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest(),
                        'architecture': {8:'mips',62:'x86_64'}.get(machine, str(machine)),
                        'byte_order': 'big' if header[5] == 2 else 'little',
                        'modinfo': info, 'undefined_symbols': imports,
                        'platform_dependencies': private,
                        'loadable_on_ffn': False})
    return {'schema': 1, 'source': str(root), 'module_count': len(modules), 'modules': modules}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.root.resolve(strict=True))
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'modules': result['module_count'], 'output': str(args.output)}))
