#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Deliver the CP's faceplate observation to the DP port owners (ffn-port-links.timer).

Reads the `faceplate` resource through the MP control daemon, the same rows
Device > Faceplate Ports shows, and ships them to the DP as one `ffn-portd
links` call: {mp_boot_id, sequence, age_seconds, lease_seconds, ports}. A
physical owner whose carrier policy is `observed` (the copper ports by
default) keeps TAP carrier and packet delivery only while the latest delivery
is fresh and says its link is up. The sequence is this MP boot's monotonic
clock in milliseconds, so a delayed or repeated delivery can never replace a
newer one on the DP. Nothing here writes to the CP or the switch.
"""
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

LEASE_SECONDS=30
DP=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=5','-o','ServerAliveInterval=3','-o','ServerAliveCountMax=2',
    '-o','UserKnownHostsFile=/etc/ffn-ngfw/plane_boot_known_hosts',
    '-o','ProxyCommand=ssh -F /etc/ffn-ngfw/ssh-cp.conf -W %h:%p ffn-cp','root@127.1.2.2']


def faceplate():
    request={'v':1,'id':str(uuid.uuid4()),'resource':'faceplate','action':'status','payload':{}}
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as conn:
        conn.settimeout(25)
        conn.connect('/run/ffn-plane-mp/control.sock')
        conn.sendall(json.dumps(request).encode()+b'\n')
        data=bytearray()
        while not data.endswith(b'\n'):
            chunk=conn.recv(65536)
            if not chunk or len(data)+len(chunk)>1024*1024:raise RuntimeError('invalid MP faceplate reply')
            data.extend(chunk)
    reply=json.loads(data)
    if not reply.get('ok'):raise RuntimeError('MP faceplate observation failed: '+str(reply.get('error'))[:200])
    return reply['result']


def deliver(rows,started):
    payload=dict(mp_boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                 sequence=int(time.monotonic()*1000),age_seconds=round(time.monotonic()-started,3),
                 lease_seconds=LEASE_SECONDS,ports=rows)
    result=subprocess.run(DP+['/usr/local/sbin/ffn-portd links'],input=json.dumps(payload),text=True,capture_output=True,timeout=40)
    if result.returncode:raise RuntimeError('DP link delivery failed: '+(result.stdout.strip() or result.stderr[-400:]))
    return json.loads(result.stdout)


def main():
    started=time.monotonic()
    rows=[dict(p) for p in faceplate().get('ports',[]) if type(p.get('port')) is int]
    print(json.dumps(deliver(rows,started)))


if __name__=='__main__':
    try:main()
    except Exception as error:
        print(json.dumps({'error':str(error)[:512]}),file=sys.stderr);sys.exit(1)
