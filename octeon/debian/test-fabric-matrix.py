#!/usr/bin/env python3
"""MP physical commissioning tests. Requires cables 1--3 and 5--13.

Temporarily changes only p1/p3/p5/p13; restores settings in finally.
Run without concurrent configuration writers. This is a paced functional test.
--restart also checks MP relay restart recovery in bridged and routed modes.
--static checks IPv4/IPv6 routes through a next hop beyond connected subnets.
--vrf checks routed forwarding inside a VRF and isolation from a second VRF.
"""
import collections
import json
import socket
import struct
import subprocess as S
import sys
import threading
import time
import uuid
sys.path.insert(0, '/usr/local/sbin')
from ffn_fabric import decode_cmh, encode_itmh

SSH = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
       '-o', 'UserKnownHostsFile=/etc/ffn-ngfw/plane_boot_known_hosts',
       '-o', 'ProxyCommand=ssh -F /etc/ffn-ngfw/ssh-cp.conf -W %h:%p ffn-cp',
       'root@127.1.2.2']


def dp(command):
    return S.check_output(SSH + [command], text=True)


def control(action, request=None):
    return json.loads(S.check_output(['/usr/local/sbin/ffn-network', action],
                                    input=json.dumps(request) if request else None, text=True))


def checksum(data):
    data += b'\0' * (len(data) % 2)
    total = sum(struct.unpack('!%dH' % (len(data)//2), data))
    while total >> 16:
        total = (total & 65535) + (total >> 16)
    return (~total) & 65535


def restart_fabric(name):
    before = control('status')
    identities = {i['ifname']: (i['ifindex'], i['address'])
                  for i in before['interfaces'] if i['ifname'] in before['config']['ports']}
    S.run(['systemctl', 'restart', 'ffn-fabric.service'], check=True, timeout=45)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        after = control('status')
        if after['running'] and set(after.get('backend', {}).get('ports', [])) == {1, 3, 5, 13}:
            links = json.loads(S.check_output(['ip', '-j', 'link', 'show'], text=True))
            if all(any(i['ifname'] == n and 'LOWER_UP' in i['flags'] for i in links)
                   for n in ('enp8s0f0', 'enp8s0f1')):
                break
        time.sleep(1)
    else:
        raise RuntimeError('fabric did not reattach after restart')
    assert after['config'] == before['config'], 'restart changed port configuration'
    assert identities == {i['ifname']: (i['ifindex'], i['address'])
                          for i in after['interfaces'] if i['ifname'] in identities}
    # Reattached TAP carrier can trigger the bridge's listening/learning states.
    # Functional probes below, rather than service status, determine recovery.
    time.sleep(32)
    print(json.dumps({'test': name+'-restart-config-preserved', 'passed': True}), flush=True)


def probe(name, ingress, output, inject, build, count=300, blocked=False, token_prefix=b''):
    assert len(token_prefix) <= 13
    token = token_prefix + uuid.uuid4().bytes[:16-len(token_prefix)]
    frames = [build(token + struct.pack('!I', i), i) for i in range(count)]
    expected = {token + struct.pack('!I', i): pair[1] for i, pair in enumerate(frames)}
    seen, arrived = collections.Counter(), collections.Counter()
    corrupt = []
    stop = threading.Event()
    errors = []
    rx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
    rx.bind(('enp8s0f1', 0))
    rx.settimeout(.1)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    rx.setsockopt(263, 1, struct.pack('IHH8s', socket.if_nametoindex('enp8s0f1'), 1, 0, b''))
    tx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    tx.bind(('enp8s0f0', 0))

    def capture():
        try:
            while not stop.is_set():
                try:
                    data, addr = rx.recvfrom(65535)
                except socket.timeout:
                    continue
                if addr[2] == socket.PACKET_OUTGOING:
                    continue
                item = decode_cmh(data)
                if not item:
                    continue
                port, frame = item
                pos = frame.find(token)
                if pos < 0:
                    continue
                key = frame[pos:pos+20]
                if port == ingress:
                    arrived[key] += 1
                if port == output:
                    seen[key] += 1
                    if frame != expected.get(key):
                        corrupt.append(key.hex())
        except BaseException as error:
            errors.append(repr(error))

    thread = threading.Thread(target=capture)
    thread.start()
    try:
        for frame, _ in frames:
            tx.send(encode_itmh(inject, frame))
            time.sleep(.005)
        time.sleep(4)  # also observe duplicates/unexpected forwarding after send
    finally:
        stop.set()
        thread.join()
        rx.close()
        tx.close()
    result = {'test': name, 'sent': count, 'ingress_seen': len(arrived),
              'returned': len(seen), 'missing': sum(k not in seen for k in expected),
              'duplicates': sum(max(0, n-1) for n in seen.values()),
              'corrupt': len(corrupt), 'expected_block': blocked, 'capture_errors': errors}
    result['passed'] = (not errors and set(arrived) == set(expected) and
                        (not seen if blocked else set(seen) == set(expected) and
                         not result['duplicates'] and not corrupt))
    print(json.dumps(result), flush=True)
    if not result['passed']:
        raise RuntimeError('physical matrix failed: ' + name)


def inspection_control(action, request=None):
    return json.loads(S.check_output(['/usr/local/sbin/ffn-inspection', action],
                                    input=json.dumps(request) if request else None, text=True))


def inspection_probe(name, build):
    before = inspection_control('status')['config']
    last = before

    def set_policy(mode, ports):
        nonlocal last
        result = inspection_control('set', {'revision': last['revision'], 'mode': mode,
                                            'ports': ports, 'literal': 'FFN_TEST_DENY'})
        last = result['accepted']
        for _ in range(10):
            state = inspection_control('status')
            if state['running'] and state['runtime']['revision'] == last['revision']:
                assert not state['runtime']['reload_error']
                return state['runtime']['counters']
            time.sleep(1)
        raise RuntimeError('inspection policy did not activate')

    try:
        start = set_policy('alert', [1])
        probe(name+'-inspection-alert', 1, 13, 3, build, count=60, token_prefix=b'FFN_TEST_DENY')
        end = inspection_control('status')['runtime']['counters']
        assert end.get('alert', 0)-start.get('alert', 0) == 60, end
        start = set_policy('block', [1])
        probe(name+'-inspection-block', 1, 13, 3, build, count=60, blocked=True, token_prefix=b'FFN_TEST_DENY')
        end = inspection_control('status')['runtime']['counters']
        assert end.get('block', 0)-start.get('block', 0) == 60, end
        probe(name+'-inspection-clean-control', 1, 13, 3, build, count=60)
        set_policy('block', [5])
        probe(name+'-inspection-port-scope', 1, 13, 3, build, count=60, token_prefix=b'FFN_TEST_DENY')
    finally:
        inspection_control('set', dict(before, revision=last['revision']))


def main():
    initial = control('status')
    assert initial['running'] and set(initial['backend']['ports']) == {1, 3, 5, 13}
    saved = {p: initial['config']['ports'][p] for p in ('p1', 'p3', 'p5', 'p13')}
    revision = initial['config']['revision']
    saved_routes = initial['config'].get('routes', [])
    saved_vrfs = initial['config'].get('vrfs', {})
    if '--vrf' in sys.argv:
        assert not saved_vrfs and not saved_routes, 'VRF lab requires no existing VRFs/static routes'
    if '--static' in sys.argv:
        assert not saved_routes, 'static lab test requires no configured static routes'
    macs = {p['ifname']: bytes.fromhex(p['address'].replace(':', ''))
            for p in initial['interfaces'] if p['ifname'] in saved}
    neighbors = []

    def patch(ports, routes=None, vrfs=None):
        nonlocal revision
        request = {'revision': revision, 'ports': ports}
        if routes is not None:
            request['routes'] = routes
        if vrfs is not None:
            request['vrfs'] = vrfs
        control('patch', request)
        revision += 1

    def l2(marker, i, vlan=None):
        hdr = b'\xff'*6 + bytes.fromhex('025220abcd81')
        hdr += b'\x88\xb5' if vlan is None else b'\x81\x00' + struct.pack('!H', vlan) + b'\x88\xb5'
        frame = hdr + marker + bytes([i % 251]) * ([64, 512, 1500][i % 3] - 20)
        return frame, frame

    try:
        # Disable cable peers' TAPs to prevent recirculation; raw captures still see them.
        access = {'mode': 'l2', 'vlans': [100], 'pvid': 100}
        patch({'p1': access, 'p3': {'mode': 'disabled'}, 'p5': access, 'p13': {'mode': 'disabled'}})
        time.sleep(32)
        probe('l2-access-mixed-sizes', 1, 13, 3, l2)
        if '--restart' in sys.argv:
            restart_fabric('l2')
            probe('l2-access-after-relay-restart', 1, 13, 3, l2)
        if '--inspection' in sys.argv:
            # L2 forwarding still contains real IPv4/UDP for the payload scanner.
            def bridged_udp(marker, i):
                payload = marker + bytes([i % 251])*64
                udp = struct.pack('!HHHH', 49000, 49001, 8+len(payload), 0)+payload
                h = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20+len(udp), i, 0, 64, 17, 0,
                                socket.inet_aton('198.18.111.1'), socket.inet_aton('198.18.111.2'))
                h = h[:10]+struct.pack('!H', checksum(h))+h[12:]
                f = b'\xff'*6+bytes.fromhex('025220abcd81')+b'\x08\x00'+h+udp
                return f, f
            inspection_probe('l2', bridged_udp)
        fdb = json.loads(dp('ip netns exec ffn-data bridge -j fdb show dev p1'))
        assert any(e['mac'] == '02:52:20:ab:cd:81' and e.get('vlan') == 100 for e in fdb)
        print(json.dumps({'test': 'physical-mac-learning', 'passed': True}), flush=True)
        trunk = {'mode': 'l2', 'vlans': [100, 200]}
        patch({'p1': trunk, 'p5': trunk})
        time.sleep(32)
        probe('l2-tagged-vlan200-mixed-sizes', 1, 13, 3, lambda m, i: l2(m, i, 200))
        probe('l2-vlan300-isolation', 1, 13, 3, lambda m, i: l2(m, i, 300), blocked=True)
        patch({'p5': {'mode': 'disabled'}})
        probe('l2-disabled-egress', 1, 13, 3, lambda m, i: l2(m, i, 200), blocked=True)

        # Move both formerly bridged ports to routed mode at runtime.
        routed_ports = {'p1': {'mode': 'l3', 'addresses': ['198.18.101.1/24', 'fd52:20:101::1/64']},
                        'p5': {'mode': 'l3', 'addresses': ['198.18.105.1/24', 'fd52:20:105::1/64']}}
        if '--vrf' in sys.argv:
            for settings in routed_ports.values():
                settings['vrf'] = 'vrf-lab-blue'
        patch(routed_ports, vrfs={'vrf-lab-blue':1001,'vrf-lab-red':1002} if '--vrf' in sys.argv else None)
        time.sleep(3)
        peer = bytes.fromhex('025220abcd82')
        for version, destination in ((4, '198.18.105.222'), (6, 'fd52:20:105::222')):
            assert not json.loads(dp('ip -%d -n ffn-data -j neigh show to %s dev p5' % (version, destination)))
            dp('ip -%d -n ffn-data neigh add %s lladdr 02:52:20:ab:cd:82 nud permanent dev p5' % (version, destination))
            neighbors.append((version, destination))
            if '--static' in sys.argv:
                prefix = '203.0.113.0/24' if version == 4 else '2001:db8:105::/64'
                patch({}, [{'dst': prefix, 'via': destination, 'dev': 'p5', 'metric': 100,
                            'table': 1001 if '--vrf' in sys.argv else 254}])
                destination = '203.0.113.222' if version == 4 else '2001:db8:105::222'

            def routed(marker, i, ttl=64):
                size = [128, 512, 1500][i % 3]
                iplen = 20 if version == 4 else 40
                payload = marker + bytes([i % 251]) * (size-iplen-8-20)
                udp = struct.pack('!HHHH', 49100, 49101, len(payload)+8, 0) + payload
                if version == 4:
                    src, dst = socket.inet_aton('198.18.101.222'), socket.inet_aton(destination)
                    pseudo = src+dst+struct.pack('!BBH', 0, 17, len(udp))
                    def header(hops):
                        h = struct.pack('!BBHHHBBH4s4s', 0x45, 0, size, i, 0x4000, hops, 17, 0, src, dst)
                        return h[:10]+struct.pack('!H', checksum(h))+h[12:]
                    ether = b'\x08\x00'
                else:
                    src = socket.inet_pton(socket.AF_INET6, 'fd52:20:101::222')
                    dst = socket.inet_pton(socket.AF_INET6, destination)
                    pseudo = src+dst+struct.pack('!I3xB', len(udp), 17)
                    def header(hops):
                        return struct.pack('!IHBB', 0x60000000, len(udp), 17, hops)+src+dst
                    ether = b'\x86\xdd'
                udp = udp[:6]+struct.pack('!H', checksum(pseudo+udp) or 65535)+udp[8:]
                frame = macs['p1']+bytes.fromhex('025220abcd81')+ether+header(ttl)+udp
                expected = peer+macs['p5']+ether+header(ttl-1)+udp
                return frame, expected

            probe(('ipv%d-static-next-hop-mixed-sizes' if '--static' in sys.argv else
                   'ipv%d-routing-after-l2-to-l3-mixed-sizes') % version, 1, 13, 3, routed)
            if '--restart' in sys.argv and version == 4:
                restart_fabric('l3')
                probe('ipv4-routing-after-relay-restart', 1, 13, 3, routed)
            if '--inspection' in sys.argv:
                inspection_probe('ipv%d' % version, routed)
            probe('ipv%d-hop-limit-one-no-forward' % version, 1, 13, 3,
                  lambda m, i: routed(m, i, 1), count=30, blocked=True)
            if '--static' in sys.argv:
                patch({}, [{'dst': prefix, 'type': 'blackhole', 'metric': 100,
                            'table': 1001 if '--vrf' in sys.argv else 254}])
                probe('ipv%d-static-blackhole' % version, 1, 13, 3, routed, count=30, blocked=True)
                patch({}, [])
        if '--vrf' in sys.argv:
            # Remove only the temporary neighbors before moving their port.
            for version, destination in neighbors:
                dp('ip -%d -n ffn-data neigh del %s dev p5' % (version, destination))
            neighbors.clear()
            red = dict(routed_ports['p5'], vrf='vrf-lab-red')
            patch({'p5': red})
            time.sleep(3)
            probe('ipv6-cross-vrf-isolation', 1, 13, 3, routed, count=60, blocked=True)
    finally:
        try:
            for version, destination in neighbors:
                dp('ip -%d -n ffn-data neigh del %s dev p5' % (version, destination))
        finally:
            patch(saved, saved_routes if '--static' in sys.argv else None,
                  saved_vrfs if '--vrf' in sys.argv else None)
            final = control('status')
            assert all(final['config']['ports'][p] == saved[p] for p in saved)
            print(json.dumps({'test': 'original-port-config-restored', 'passed': True,
                              'revision': final['config']['revision']}), flush=True)


if __name__ == '__main__':
    main()
