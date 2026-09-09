#!/usr/bin/env python3
"""CP: wait for BCM init, apply front-port rates, verify admin state."""
import json
import socket
import time

def call(request):
    with socket.create_connection(('127.1.1.2', 8104), timeout=5) as sock:
        sock.settimeout(190)
        with sock.makefile('rwb') as stream:
            stream.write((json.dumps(request) + '\n').encode())
            stream.flush()
            result = json.loads(stream.readline())
    if not result.get('ok'):
        raise RuntimeError(result)
    return result

deadline = time.monotonic() + 900
while True:
    try:
        status = call({'op': 'status'})
    except OSError:
        status = {'state': 'starting'}
    if status['state'] == 'ready':
        if status.get('init_errors'):
            raise RuntimeError(status)
        break
    if status['state'] == 'dead' or time.monotonic() >= deadline:
        raise RuntimeError(status)
    time.sleep(2)

result = call({'op': 'cint.run', 'script': 'ffn_bcm_front_init.c'})
print(json.dumps(result), flush=True)
if not result.get('completed'):
    raise RuntimeError('front-port configuration failed')
ports = call({'op': 'port.list'})['ports']
front = [p for p in ports if p['faceplate'] and p['port'] != 12]
if len(front) != 24 or not all(p['enabled'] for p in front):
    raise RuntimeError('not all 24 front ports are enabled')
print('Configured 24 front ports. Link requires a compatible connected peer.', flush=True)
for p in front:
    print(p['raw'], flush=True)
