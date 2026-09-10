#!/usr/bin/env python3
"""DP kernel protocol tests in disposable namespaces, not physical-port tests.
MACsec keys are random, ephemeral and never printed or saved.
"""
import json
import os
import socket
import sys
import subprocess as S
import threading
import time

A, B = 'ffn-vtest-a', 'ffn-vtest-b'


def run(*args, check=True):
    p = S.run(args, capture_output=True, text=True)
    if check and p.returncode:
        # Never include argv: MACsec provisioning calls contain ephemeral keys.
        raise RuntimeError('protocol test command failed: '+p.stderr.strip())
    return p


def ip(ns, *args):
    return run('ip', '-n', ns, *args).stdout


def keycmd(ns, *args):
    return run('ip', 'netns', 'exec', ns, 'ip', 'macsec', *args).stdout


def ping(ns, destination, count=100, success=True):
    if success:
        # Exclude ARP/neighbor and lazy crypto-module startup from measured bursts.
        run('ip', 'netns', 'exec', ns, '/usr/bin/busybox', 'ping', '-c', '1', '-W', '2', destination)
    p = run('ip', 'netns', 'exec', ns, '/usr/bin/busybox', 'ping', '-c', str(count),
            '-i', '.02', '-W', '2', '-s', '1372', '-p', 'd3', destination, check=False)
    if success:
        assert p.returncode == 0 and '0% packet loss' in p.stdout and '100% packet loss' not in p.stdout, p.stdout
    else:
        assert p.returncode != 0 and '100% packet loss' in p.stdout, p.stdout


def main():
    existing = {n['name'] for n in json.loads(run('ip', '-j', 'netns', 'list').stdout)}
    assert not existing.intersection((A, B)), 'test namespaces already exist'
    made = []
    try:
        for ns in (A, B):
            run('ip', 'netns', 'add', ns)
            made.append(ns)
            ip(ns, 'link', 'set', 'lo', 'up')
        ip(A, 'link', 'add', 'ua', 'type', 'veth', 'peer', 'name', 'ub', 'netns', B)
        for ns, dev, octet in ((A, 'ua', 1), (B, 'ub', 2)):
            ip(ns, 'link', 'set', dev, 'address', '02:52:20:10:00:0%d' % octet)
            ip(ns, 'address', 'add', '198.18.240.%d/24' % octet, 'dev', dev)
            ip(ns, 'link', 'set', dev, 'up')
        for kind in (() if '--macsec-only' in sys.argv else ('vxlan', 'geneve', 'gretap', 'gre', 'ipip')):
            for ns, dev, local, remote in ((A, 'ua', 1, 2), (B, 'ub', 2, 1)):
                args = ['link', 'add', 'tun', 'type', kind, 'remote', '198.18.240.%d' % remote]
                if kind != 'geneve':
                    args += ['local', '198.18.240.%d' % local, 'dev', dev]
                if kind in ('vxlan', 'geneve'):
                    args += ['id', '5220', 'dstport', '4789' if kind == 'vxlan' else '6081']
                elif kind in ('gre', 'gretap'):
                    args += ['key', '5220']
                ip(ns, *args)
                ip(ns, 'link', 'set', 'tun', 'mtu', '1400', 'up')
                ip(ns, 'address', 'add', '198.18.241.%d/24' % local, 'dev', 'tun')
            ping(A, '198.18.241.2')
            ping(B, '198.18.241.1')
            print(json.dumps({'test': kind, 'packets_each_direction': 100, 'inner_ip_bytes': 1400, 'passed': True}), flush=True)
            for ns in (A, B):
                ip(ns, 'link', 'delete', 'tun')

        key = os.urandom(16).hex()
        for ns, dev, local, remote in ((A, 'ua', 1, 2), (B, 'ub', 2, 1)):
            ip(ns, 'link', 'add', 'link', dev, 'name', 'sec', 'type', 'macsec', 'port', '1',
               'encrypt', 'on', 'protect', 'on', 'validate', 'strict', 'replay', 'on', 'window', '64')
            keycmd(ns, 'add', 'sec', 'tx', 'sa', '0', 'pn', '1', 'on', 'key', '01', key)
            sci = '02522010000%d0001' % remote
            keycmd(ns, 'add', 'sec', 'rx', 'sci', sci, 'on')
            keycmd(ns, 'add', 'sec', 'rx', 'sci', sci, 'sa', '0', 'pn', '1', 'on', 'key', '01', key)
            ip(ns, 'link', 'set', 'sec', 'mtu', '1400', 'up')
            ip(ns, 'address', 'add', '198.18.241.%d/24' % local, 'dev', 'sec')
        original = os.open('/proc/self/ns/net', os.O_RDONLY)
        target = os.open('/run/netns/'+A, os.O_RDONLY)
        try:
            os.setns(target, 0)
            capture = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
            capture.bind(('ua', 0))
            capture.settimeout(.1)
        finally:
            os.setns(original, 0)
            os.close(original)
            os.close(target)
        counts = {'encrypted_frames': 0, 'plaintext_leaks': 0}
        stop = threading.Event()

        def read():
            while not stop.is_set():
                try:
                    frame = capture.recv(65535)
                except socket.timeout:
                    continue
                if frame[12:14] != b'\x88\xe5':
                    continue
                counts['encrypted_frames'] += 1
                counts['plaintext_leaks'] += b'\xd3'*32 in frame

        thread = threading.Thread(target=read)
        thread.start()
        try:
            ping(A, '198.18.241.2')
            ping(B, '198.18.241.1')
        finally:
            stop.set()
            thread.join()
            capture.close()
        assert counts['encrypted_frames'] >= 100 and not counts['plaintext_leaks'], counts
        print(json.dumps({'test': 'macsec-encrypted-traffic', 'packets_each_direction': 100,
                          'replay_window': 64, **counts, 'passed': True}), flush=True)
        keycmd(B, 'set', 'sec', 'rx', 'sci', '0252201000010001', 'sa', '0', 'off')
        keycmd(B, 'del', 'sec', 'rx', 'sci', '0252201000010001', 'sa', '0')
        keycmd(B, 'add', 'sec', 'rx', 'sci', '0252201000010001', 'sa', '0', 'pn', '1', 'on', 'key', '01', os.urandom(16).hex())
        ping(A, '198.18.241.2', count=3, success=False)
        print(json.dumps({'test': 'macsec', 'packets_each_direction': 100, **counts,
                          'wrong_key_rejected': True, 'passed': True}), flush=True)
    finally:
        for ns in reversed(made):
            run('ip', 'netns', 'delete', ns)


if __name__ == '__main__':
    main()
