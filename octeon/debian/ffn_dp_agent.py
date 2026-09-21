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
import sys
import time
import uuid
from ffn_dp_boot_health import inspect_boot

SOCKET = '/run/ffn-dp-agent/control.sock'
sys.path.insert(0, '/usr/local/lib/ffn')


def vif_status():
    """Observe the running owner only; never instantiate or reconcile a VIF."""
    if not Path('/run/ffn-vif.sock').exists():
        return {'available': False, 'running': False, 'forwarding': False}
    try:
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(3)
            conn.connect('/run/ffn-vif.sock')
            conn.sendall(b'{"op":"status","payload":{}}\n')
            with conn.makefile('rb') as stream:
                raw = stream.readline(1048577)
        if len(raw) > 1048576 or not raw.endswith(b'\n'):
            raise ValueError('invalid VIF response')
        result = json.loads(raw)
        if result.get('ok') is not True:
            raise ValueError('VIF owner unavailable')
        value = result['result']
        return {'available': True, **{key: value.get(key) for key in
            ('running', 'forwarding', 'generation', 'runtime_error', 'recovery_required',
             'vif_links', 'counters')}, 'revision': value.get('config', {}).get('revision')}
    except (OSError, ValueError, KeyError, TypeError):
        return {'available': False, 'running': None, 'forwarding': False,
                'error': 'VIF observation unavailable'}


def policy_status():
    """Observe the actual supervisor/kernel ACK, never infer it from boot."""
    try:
        from ffn_security_runtime import status
        value=status()
        return {key:value[key] for key in ('available','applied','revision','provider','digest','processing','nat','error') if key in value}
    except (ImportError,OSError,ValueError,RuntimeError,KeyError):
        return {'available':False,'applied':False,'error':'Security/NAT acknowledgment unavailable'}


def snapshot(nonce):
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    cpu = Path('/proc/cpuinfo').read_text().lower()
    octeon = 'octeon' in cpu
    cores = sum(line.startswith('processor') and ':' in line for line in cpu.splitlines())
    dp_hardware = octeon and cores >= 16
    boot_health = inspect_boot()
    ready = dp_hardware and boot_health['ready']
    packet_io = {'available':False, 'error':'DP link observer is not installed'}
    packet_initialization = {'available':False}
    if dp_hardware:
        try:
            from ffn_dp_link import observe
            packet_io = observe()
        except ImportError:
            pass
        try:
            from ffn_dp_packet_init import status as packet_status
            packet_initialization = packet_status()
        except (ImportError, OSError, RuntimeError, ValueError):
            packet_initialization = {'available':False,'error':'packet initialization status unavailable'}
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
            'octeon': octeon, 'cpu_count': cores, 'ready': ready, 'observed_at': time.time(),
            'boot': boot_health,
            'packet_io': packet_io,
            'packet_initialization': packet_initialization,
            'state': ('wrong-hardware' if not dp_hardware else 'boot-incomplete' if not ready
                      else 'inspection-active' if runtime and runtime.get('mode')!='off' else 'ready'),
            'policy_processing':policy_status(),
            'forwarding_verified': False,
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
    parser.add_argument('action', choices=['serve', 'status', 'diagnose', 'stream'])
    args = parser.parse_args()
    if args.action == 'stream':
        from ffn_agent_protocol import serve
        def report():
            # Observe the managed readiness agent, not a second independent
            # readiness process. VIF counters come from the packet owner.
            value = handshake()
            value['vifs'] = vif_status()
            return value
        serve(report, 'dp', 'pa5200')
        return
    if args.action == 'diagnose':
        print(json.dumps(snapshot(str(uuid.uuid4())), indent=2))
        return
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
