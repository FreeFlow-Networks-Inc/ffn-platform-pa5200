#!/usr/bin/env python3
"""PA-5200 DP readiness handshake over a root-only local socket and pinned SSH."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import platform
import socket
import socketserver
import time
import uuid

SOCKET = '/run/ffn-dp-agent/control.sock'


def snapshot(nonce):
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    cpu = Path('/proc/cpuinfo').read_text().lower()
    octeon = 'octeon' in cpu
    engines = []
    try:
        lib = ctypes.CDLL('/usr/local/lib/libffn-inline.so')
        lib.ffn_inline_scan
        engines.append('literal')
        if hasattr(lib, 'ffn_inline_create_profile'):
            engines.extend(['credit_card', 'ssn', 'api_key'])
    except (OSError, AttributeError):
        pass
    runtime = None
    try:
        observed = json.loads(Path('/run/ffn-inspection.json').read_text())
        if (type(observed.get('pid')) is int and observed['pid'] > 0 and
                Path('/proc', str(observed['pid'])).exists() and
                0 <= time.time() - observed['updated_at'] < 5):
            runtime = observed
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return {'protocol': 1, 'nonce': nonce, 'role': 'dataplane', 'platform': 'pa5200',
            'boot_id': boot, 'agent_pid': os.getpid(), 'arch': platform.machine(),
            'octeon': octeon, 'ready': octeon, 'observed_at': time.time(),
            'state': 'forwarding' if octeon and runtime else 'ready' if octeon else 'wrong-hardware',
            'engines': {'execution': 'OCTEON CPU', 'available': engines,
                        'runtime': runtime, 'hardware_acceleration': False,
                        'limitations': ['2048-byte packet payload budget', 'no stream reassembly',
                                        'no TLS decryption', 'fragments and unsupported protocols pass']}}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        data = self.rfile.readline(4097)
        if len(data) > 4096:
            return
        try:
            request = json.loads(data)
            if set(request) != {'protocol', 'nonce'} or request['protocol'] != 1:
                return
            nonce = str(uuid.UUID(request['nonce']))
            self.wfile.write(json.dumps(snapshot(nonce)).encode() + b'\n')
        except (ValueError, TypeError, OSError):
            return


def handshake(path=SOCKET):
    nonce = str(uuid.uuid4())
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(7)
        client.connect(path)
        client.sendall(json.dumps({'protocol': 1, 'nonce': nonce}).encode() + b'\n')
        with client.makefile('rb') as stream:
            data = stream.readline(65537)
        if len(data) > 65536:
            raise ValueError('oversized handshake')
    result = json.loads(data)
    if result.get('nonce') != nonce or result.get('protocol') != 1 or result.get('role') != 'dataplane':
        raise ValueError('invalid handshake response')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['serve', 'status'])
    args = parser.parse_args()
    if args.action == 'status':
        print(json.dumps(handshake()))
        return
    import fcntl
    os.umask(0o077)
    Path(SOCKET).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(SOCKET + '.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        Path(SOCKET).unlink(missing_ok=True)
        with socketserver.UnixStreamServer(SOCKET, Handler) as server:
            os.chmod(SOCKET, 0o600)
            server.serve_forever()


if __name__ == '__main__':
    main()
