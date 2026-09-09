#!/usr/bin/env python3
"""Read FE100 physical link status; does not assert packet forwarding readiness."""
import argparse
import json
import socket
import time
from ffn_fe100 import Fe100

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--require-up', action='store_true')
args = p.parse_args()
deadline = time.monotonic() + (30 if args.require_up else 0)
while True:
    with socket.create_connection(('127.1.1.2', 8104), timeout=10) as s:
        s.settimeout(30)
        with s.makefile('rwb') as stream:
            stream.write(b'{"op":"port.list"}\n'); stream.flush()
            reply = json.loads(stream.readline())
    if not reply.get('ok'): raise RuntimeError(reply)
    ports = [p for p in reply['ports'] if p['port'] in (3, 20)]
    fe = Fe100()
    try:
        regs = {name: fe.read32(off) for name, off in (
            ('nif_pcs', 0x10030), ('tmi_il', 0x8014),
            ('tmi_init', 0x8010), ('nif_pll', 0x10a14), ('tmi_pll', 0x8614))}
    finally:
        fe.close()
    up = (len(ports) == 2 and all(p['link'] for p in ports)
          and bool(regs['nif_pcs'] & 0x10)
          and (regs['tmi_il'] & 0x1fff) == 0x1fff
          and bool(regs['nif_pll'] & 1) and bool(regs['tmi_pll'] & 1))
    if up or time.monotonic() >= deadline: break
    time.sleep(1)
print(json.dumps({'physical_links_up': up, 'ports': ports,
                  'registers': {k: hex(v) for k, v in regs.items()}}, indent=2))
raise SystemExit(0 if up or not args.require_up else 1)
