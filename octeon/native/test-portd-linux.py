#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""ffn-portd against the MP contracts, on a Linux host with root: two disposable
network namespaces, a veth standing in for the OCTEON trunk, the real native packet
library on real TAPs, and the Python reference engine as the LACP peer.

One daemon owns every port (management/PORT-TABLE.md). Proves, without an
appliance: the daemon starts with every port disabled and publishes its table
status; a pushed table attaches an l3 port within seconds and `physical status`
reads it as running; the copper carrier lease follows deliveries through the
daemon's socket; an aggregate lease through the `aggregate serve` relay
negotiates both members against the reference engine, sends the first status
row within the startup budget, and ends with its gates withdrawn, its TAP torn
down and its run files gone when the pipe closes, when the controller falls
silent, and when the relay is terminated; the table refuses to take a live
member and a lease refuses an l3 port; a daemon killed outright comes back on
its saved table with the port re-attached; the watchdog sweep retires a frozen
daemon; SIGTERM stops everything cleanly.

The kernel-interface helpers are a fake runtime script in a temporary directory:
the real ones need the appliance's namespaces, nftables tables and management
profiles. The CPU reservation and steering helpers are faked the same way.
Nothing outside the two namespaces and the temporary directory is touched.
"""
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid

HERE = Path(__file__).resolve().parent
DEBIAN = HERE.parent / 'debian'
sys.path.insert(0, str(DEBIAN))
FRONT = dict(enumerate((16, 1, 18, 19, 6, 21, 22, 23, 7, 11, 36, 27, 10, 29, 30, 31, 32, 33, 34, 35), 5))
SYSTEM = '02:00:00:00:20:01'
PEER = '02:00:00:00:20:02'
MEMBERS = [5, 6]
BOOT = Path('/proc/sys/kernel/random/boot_id').read_text().strip()

FAKE_RUNTIME = '''#!/usr/bin/env python3
import json, os, subprocess, sys
log = open(os.environ['FAKE_LOG'], 'a')
verb = sys.argv[1]
request = json.load(sys.stdin)
log.write(json.dumps({'verb': verb, 'request': request}) + '\\n'); log.flush()
ns = os.environ['FAKE_NS']
if verb == 'validate':
    print(json.dumps({'ok': True}))
elif verb == 'network-setup':
    name = request['intent']['group']
    subprocess.run(['ip', '-n', ns, 'tuntap', 'add', 'dev', name, 'mode', 'tap', 'multi_queue'], check=True)
    subprocess.run(['ip', '-n', ns, 'link', 'set', name, 'address', request['intent']['system'], 'mtu', '1500', 'up'], check=True)
    print(json.dumps({'ok': True, 'subinterfaces': []}))
elif verb == 'network-teardown':
    subprocess.run(['ip', '-n', ns, 'link', 'delete', request['group']], check=False)
    print(json.dumps({'ok': True}))
elif verb == 'network-update':
    print(json.dumps({'ok': True, 'revision': request.get('revision', ''), 'subinterfaces': []}))
else:
    print(json.dumps({'error': 'unknown verb ' + verb})); sys.exit(1)
'''

FAKE_CPUS = '''#!/usr/bin/env python3
import json, os, sys
count = int(sys.argv[sys.argv.index('--count') + 1])
cpus = sorted(os.sched_getaffinity(0))
print(json.dumps({'token': '0123456789abcdef0123456789abcdef', 'cpus': [cpus[i % len(cpus)] for i in range(count)]}))
'''

FAKE_STEERING = '#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps({"device": sys.argv[1], "cpus": [], "mask": "0"}))\n'


def run(*args, **kw):
    return subprocess.run(list(args), check=True, capture_output=True, text=True, timeout=20, **kw).stdout


def intent(token, control_only=False):
    return dict(group='ae1', token=token, boot_id=BOOT, system=SYSTEM, members=list(MEMBERS),
                lacp=dict(activity='active', rate='fast', min_links=1, system_priority=32768),
                network=dict(enabled=True, addresses=[], dhcp=False, dhcp_default_route=False, dhcp_route_metric=10,
                             mtu=1500, management=dict(profile='', ping=False, tcp=[], udp=[], sources=[]), units=[]),
                lldp=False, control_only=control_only, offload=False)


def peer_worker(directory):
    """The LACP partner: the Python reference engine on the trunk peer, inside the trunk namespace.

    Frames the owner injects (0x01, BE16 BCM port, 0x00, MACs, eight RAW_DSA bytes, rest) are
    unwrapped by port; the partner's own LACPDUs are delivered as the switch would, behind
    the four-byte OTMH prefix (0x0018 then the BCM source port)."""
    from ffn_lacp_engine import Engine

    class Gates:
        def apply(self, value): return value
    reverse = {v: k for k, v in FRONT.items()}
    engine = Engine(PEER, 7, {p: PEER for p in MEMBERS}, Gates())
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
    sock.bind(('peer', 0)); sock.setblocking(False)
    deadline = time.monotonic() + 300
    next_report = 0
    while time.monotonic() < deadline:
        now = time.monotonic()
        for port in MEMBERS: engine.link(port, True, 10000, now)
        for port, frame in engine.transmissions(now):
            sock.send(b'\0\x18' + struct.pack('!H', FRONT[port]) + frame)
        if now >= next_report:
            (directory / 'peer.json').write_text(json.dumps(engine.status(now)))
            next_report = now + 0.2
        try:
            while True:
                raw, address = sock.recvfrom(4096)
                if address[2] == socket.PACKET_OUTGOING or len(raw) < 28 or raw[0] != 1 or raw[3] != 0: continue
                port = reverse.get(struct.unpack_from('!H', raw, 1)[0])
                if port not in MEMBERS: continue
                frame = raw[4:16] + raw[24:]
                if frame[12:14] == b'\x88\x09': engine.receive(port, frame, time.monotonic())
        except BlockingIOError:
            pass
        time.sleep(0.05)


class Daemon:
    """`ffn-portd serve` in the trunk namespace, with its command lines as clients."""
    def __init__(self, env, trunk_ns, rundir, log):
        self.env, self.trunk_ns, self.rundir = env, trunk_ns, rundir
        self.log = open(log, 'a')
        self.process = subprocess.Popen(['ip', 'netns', 'exec', trunk_ns, str(HERE / 'ffn-portd'), 'serve'],
                                        stdout=self.log, stderr=subprocess.STDOUT, env=env)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not (rundir / 'ffn-portd.sock').exists():
            if self.process.poll() is not None: raise RuntimeError('daemon exited at start: ' + Path(log).read_text()[-1500:])
            time.sleep(0.05)
        if not (rundir / 'ffn-portd.sock').exists(): raise RuntimeError('daemon never listened')

    def cli(self, *args, stdin=None, timeout=40):
        out = subprocess.run(['ip', 'netns', 'exec', self.trunk_ns, str(HERE / 'ffn-portd')] + list(args), env=self.env,
                             input=None if stdin is None else json.dumps(stdin), capture_output=True, text=True, timeout=timeout)
        try: return out.returncode, json.loads(out.stdout.strip().splitlines()[-1]) if out.stdout.strip() else {}
        except ValueError: raise RuntimeError('non-JSON reply: %r %r' % (out.stdout, out.stderr))

    def table(self, rows, generation='test'):
        ports = {str(p): {'mode': 'disabled'} for p in range(1, 25)}
        for port, row in rows.items(): ports[str(port)] = row
        return self.cli('table', 'apply', stdin=dict(boot_id=BOOT, generation=generation, ports=ports))

    def status(self):
        rc, doc = self.cli('table', 'status')
        assert rc == 0, doc
        return doc

    def wait(self, predicate, seconds, what):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.process.poll() is not None: raise RuntimeError('daemon exited: ' + open(self.log.name).read()[-2000:])
            if predicate(): return
            time.sleep(0.1)
        raise RuntimeError('timeout waiting for ' + what + ': ' + json.dumps(self.status())[:1500])

    def stop(self, seconds=20):
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        try: return self.process.wait(timeout=seconds)
        except subprocess.TimeoutExpired: self.process.kill(); raise


class Owner:
    """One aggregate lease under the MP contract, through the `aggregate serve` relay."""
    def __init__(self, env, trunk_ns, token, control_only=False):
        self.token = token
        self.process = subprocess.Popen(['ip', 'netns', 'exec', trunk_ns, str(HERE / 'ffn-portd'), 'aggregate', 'serve'],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(env['FAKE_LOG'] + '.relay', 'a'),
                                        env=env, bufsize=0)
        os.set_blocking(self.process.stdout.fileno(), False)
        self.buffer = b''; self.rows = []; self.sequence = 0
        self.send(intent(token, control_only))

    def send(self, message):
        self.process.stdin.write((json.dumps(message) + '\n').encode()); self.process.stdin.flush()

    def observe(self, up=True):
        self.sequence += 1
        self.send(dict(token=self.token, sequence=self.sequence, age_seconds=0.01,
                       links=[dict(port=p, up=up, speed_mbps=10000 if up else 0) for p in MEMBERS], offload=None))

    def read(self):
        try:
            data = os.read(self.process.stdout.fileno(), 1 << 20)
        except BlockingIOError:
            return
        self.buffer += data
        while b'\n' in self.buffer:
            line, self.buffer = self.buffer.split(b'\n', 1)
            self.rows.append(json.loads(line))

    def wait_row(self, predicate, seconds, heartbeat=True):
        deadline = time.monotonic() + seconds; next_beat = 0
        while time.monotonic() < deadline:
            if heartbeat and time.monotonic() >= next_beat: self.observe(); next_beat = time.monotonic() + 0.5
            self.read()
            for row in self.rows:
                if predicate(row): return row
            if self.process.poll() is not None:
                raise RuntimeError('relay exited early: ' + json.dumps(self.rows[-3:]))
            time.sleep(0.05)
        raise RuntimeError('timeout: ' + json.dumps(self.rows[-2:]))

    def wait_exit(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.read()
            if self.process.poll() is not None: return self.process.returncode
            time.sleep(0.05)
        self.process.kill(); raise RuntimeError('relay did not exit within %ss' % seconds)


def test():
    if os.geteuid() != 0: raise RuntimeError('root required for isolated namespaces')
    if not (HERE / 'ffn-portd').exists() or not (HERE / 'libffn-packet.so').exists():
        raise RuntimeError('build ffn-portd and libffn-packet.so first')
    # The native library bounds names at IFNAMSIZ; the namespace name is one of them.
    tag = str(os.getpid() % 10000)
    trunk_ns, data_ns = 'agt' + tag, 'agd' + tag
    made = []; peer = None; daemon = None; results = {}
    with tempfile.TemporaryDirectory(prefix='ffn-portd-') as temp:
        directory = Path(temp); rundir = directory / 'run'; rundir.mkdir()
        fake = directory / 'fake_runtime.py'; fake.write_text(FAKE_RUNTIME); fake.chmod(0o755)
        cpus = directory / 'fake_cpus.py'; cpus.write_text(FAKE_CPUS); cpus.chmod(0o755)
        steering = directory / 'fake_steering.sh'; steering.write_text(FAKE_STEERING); steering.chmod(0o755)
        log = directory / 'helpers.log'; log.touch()
        env = dict(os.environ, FFN_PORTD_UNCHECKED='1', FFN_PORTD_RUN=str(rundir), FFN_PORTD_NS=data_ns,
                   FFN_PORTD_PYTHON=sys.executable, FFN_PORTD_RUNTIME=str(fake), FFN_PORTD_CPUS=str(cpus),
                   FFN_PORTD_STEERING=str(steering), FFN_PORTD_CPU_RESERVATIONS=str(directory / 'cpus'),
                   FFN_PORTD_INSPECTION=str(directory / 'absent-inspection.json'),
                   FFN_PORTD_FE100_RETURN=str(directory / 'absent-fe100.json'),
                   FFN_PORTD_LANES=str(directory / 'absent-lanes.json'),
                   FFN_PORTD_NETWORK=str(directory / 'network.json'), FFN_PORTD_ROUTE_LINKS=str(directory / 'route-links.json'),
                   LD_LIBRARY_PATH=str(HERE), FAKE_LOG=str(log), FAKE_NS=data_ns)
        (directory / 'network.json').write_text(json.dumps(dict(revision=7, ports={}, routes=[])))
        try:
            for ns in (trunk_ns, data_ns):
                run('ip', 'netns', 'add', ns); made.append(ns); run('ip', '-n', ns, 'link', 'set', 'lo', 'up')
            run('ip', '-n', trunk_ns, 'link', 'add', 'ffnpkt0', 'type', 'veth', 'peer', 'name', 'peer')
            for name in ('ffnpkt0', 'peer'): run('ip', '-n', trunk_ns, 'link', 'set', name, 'mtu', '1600', 'up')
            # The TAPs the network owner makes at boot: ports 3, 5 and 6 are enough here.
            for port in (3, 5, 6):
                run('ip', '-n', data_ns, 'tuntap', 'add', 'dev', 'p%d' % port, 'mode', 'tap', 'multi_queue')
                run('ip', '-n', data_ns, 'link', 'set', 'p%d' % port, 'up')
            peer = subprocess.Popen(['ip', 'netns', 'exec', trunk_ns, sys.executable, str(Path(__file__).resolve()), '--peer', str(directory)],
                                    stdout=open(directory / 'peer.log', 'w'), stderr=subprocess.STDOUT)

            def carrier(port):
                link = json.loads(run('ip', '-n', data_ns, '-d', '-j', 'link', 'show', 'dev', 'p%d' % port))[0]
                return 'NO-CARRIER' not in link.get('flags', [])

            def physical_status(port):
                rc, doc = daemon.cli('physical', 'status', '--port', str(port))
                return doc

            # 1. The daemon starts with every port disabled and publishes its table status.
            daemon = Daemon(env, trunk_ns, rundir, directory / 'daemon.log')
            status = daemon.status()
            assert status['owner'] == 'ffn-portd' and status['boot_id'] == BOOT and len(status['ports']) == 24, status
            assert all(row['desired'] == 'disabled' and not row['attached'] for row in status['ports'].values()), status['ports']
            assert status['revision'] == 0 and status['aggregates'] == {}, status
            results['started'] = dict(pid=status['pid'])
            # 2. A pushed table attaches an l3 port; the status document and `physical status` read it as running.
            rc, reply = daemon.table({5: {'mode': 'l3'}})
            assert rc == 0 and reply['ok'] and reply['revision'] == 1 and reply['rejected'] == {}, reply
            status_path = rundir / 'ffn-physical-5-status.json'
            daemon.wait(lambda: status_path.exists() and physical_status(5)['running'] is True, 15, 'port 5 attached')
            row = json.loads(status_path.read_text())
            link = json.loads(run('ip', '-n', data_ns, '-j', 'link', 'show', 'dev', 'p5'))[0]
            assert row['owner'] == 'physical-5' and row['pid'] == daemon.process.pid and row['ports'] == [5] and row['packet_execution'] == 'native-c', row
            assert row['interfaces'] == {'p5': link['ifindex']} and row['carrier']['policy'] == 'always' and 'rx_p5' in row['counters'], row
            assert carrier(5)
            table_status = daemon.status()['ports']['5']
            assert table_status['attached'] and table_status['reason'] == 'running' and table_status['owner'] == 'physical-5', table_status
            assert json.loads((rundir / 'ffn-portd-table.json').read_text())['ports']['5'] == {'mode': 'l3'}
            results['table_attach'] = dict(ifindex=link['ifindex'], workers=row['workers']['count'])
            # 3. A table row for a port without a TAP keeps retrying with a reason, and never blocks the others.
            rc, reply = daemon.table({5: {'mode': 'l3'}, 9: {'mode': 'l3'}})
            assert rc == 0 and reply['ok'], reply
            daemon.wait(lambda: daemon.status()['ports']['9']['failures'] >= 2, 15, 'port 9 retried')
            row9 = daemon.status()['ports']['9']
            assert not row9['attached'] and 'attach failed' in row9['reason'] and physical_status(5)['running'] is True, row9
            results['retry'] = dict(reason=row9['reason'][:80], failures=row9['failures'])
            # 4. `physical stop|start --port 5` through the socket: detach, then attach again.
            rc, reply = daemon.cli('physical', 'stop', '--port', '5', stdin={'boot_id': BOOT})
            assert rc == 0 and reply['running'] is False and not status_path.exists(), reply
            assert daemon.status()['ports']['5']['desired'] == 'disabled'
            rc, reply = daemon.cli('physical', 'start', '--port', '5', stdin={'boot_id': BOOT})
            assert rc == 0 and reply['running'] is True and reply['attachment']['owner'] == 'physical-5', reply
            assert daemon.status()['ports']['5']['desired'] == 'l3' and daemon.status()['ports']['5']['attached']
            rc, reply = daemon.cli('physical', 'start', '--port', '5', stdin={'boot_id': 'not-this-boot'})
            assert rc == 1 and 'boot identity' in reply['error'], reply
            results['physical_verbs'] = True
            # 5. A copper port: carrier follows the delivered faceplate observation under its lease, through the daemon.
            rc, reply = daemon.table({5: {'mode': 'l3'}, 3: {'mode': 'l3'}})
            assert rc == 0 and reply['ok'], reply
            status3 = rundir / 'ffn-physical-3-status.json'
            daemon.wait(lambda: status3.exists() and physical_status(3)['running'] is True, 15, 'port 3 attached')
            row = json.loads(status3.read_text())
            assert row['owner'] == 'physical-3' and row['carrier']['policy'] == 'observed' and row['carrier']['up'] is False, row['carrier']
            assert not carrier(3), 'copper TAP had carrier before any observation'
            sequence = 1000

            def deliver(link_up, lease=4, seq=None, speed=1000):
                nonlocal sequence
                if seq is None: sequence += 1
                use = sequence if seq is None else seq   # an explicit old sequence is a replay, not the new high mark
                row3 = dict(port=3, media='copper', available=True, enabled=True, phy_enabled=True, mac_enabled=True,
                            link=link_up, mac_link=link_up, datapath_link=link_up, phy_pending=False, speed_mbps=speed, mac_speed_mbps=1000)
                payload = dict(mp_boot_id='mp-boot-test', sequence=use, age_seconds=0.2, lease_seconds=lease, ports=[row3, dict(port=5, media='sfp', available=True, enabled=True, link=True)])
                return daemon.cli('links', stdin=payload)
            rc, accepted = deliver(True)
            assert rc == 0 and accepted['accepted'] is True and accepted['route_links'] is True, accepted
            observation = json.loads((directory / 'route-links.json').read_text())
            assert observation['boot_id'] == BOOT and observation['revision'] == 7 and observation['links'] == {'p3': True, 'p5': True}, observation
            daemon.wait(lambda: carrier(3), 5, 'carrier up after the observation')
            assert json.loads(status3.read_text())['carrier']['up'] is True
            rc, stale = deliver(True, seq=sequence - 1)
            assert rc != 0 and 'stale' in stale['error'], stale
            assert deliver(True, speed=100)[0] == 0   # PHY and MAC speeds disagree: not ready
            daemon.wait(lambda: not carrier(3), 5, 'carrier down on a speed mismatch')
            assert 'speeds' in json.loads(status3.read_text())['carrier']['reason']
            assert deliver(False)[0] == 0 and json.loads((directory / 'route-links.json').read_text())['links'] == {'p3': False, 'p5': True}
            assert deliver(True)[0] == 0
            daemon.wait(lambda: carrier(3), 5, 'carrier up again')
            lapsed = time.monotonic()
            daemon.wait(lambda: not carrier(3), 9, 'lease lapse')
            results['copper_carrier'] = dict(lease_lapse_seconds=round(time.monotonic() - lapsed, 1), reason=json.loads(status3.read_text())['carrier']['reason'])
            # 6. An aggregate lease through the relay: the table makes 5 and 6 members first (5 is detached for it).
            rc, reply = daemon.table({3: {'mode': 'l3'}, 5: {'mode': 'aggregate', 'group': 'ae1'}, 6: {'mode': 'aggregate', 'group': 'ae1'}})
            assert rc == 0 and reply['ok'] and reply['rejected'] == {}, reply
            daemon.wait(lambda: not status_path.exists() and daemon.status()['ports']['5']['reason'].startswith('reserved for ae1'), 10, 'port 5 reserved')
            token = str(uuid.uuid4())
            owner = Owner(env, trunk_ns, token)
            first = owner.wait_row(lambda r: 'token' in r, 30)
            assert 'error' not in first, first
            assert first['token'] == token and first['boot_id'] == BOOT and first['owner'] == 'ffn-portd' and first['pid'] == daemon.process.pid, first
            assert first['packet_execution'] == 'native-c' and first['attachment_ready'] is True and first['configuration_ready'] is True, first
            negotiated = owner.wait_row(lambda r: r.get('distributing') == MEMBERS, 25)
            peer_status = json.loads((directory / 'peer.json').read_text())
            assert peer_status['distributing'] == MEMBERS, peer_status
            assert all(m['rx'] == 'current' and m['matched'] for m in negotiated['members'].values()), negotiated['members']
            assert (rundir / 'ffn-aggregate-ae1-status.json').exists() and (rundir / 'ffn-aggregate-ae1-intent.json').exists()
            assert 'ae1' in run('ip', '-n', data_ns, 'link')
            table_status = daemon.status()
            assert table_status['aggregates']['ae1']['stage'] == 'running' and table_status['aggregates']['ae1']['distributing'] == MEMBERS, table_status['aggregates']
            assert table_status['ports']['5']['member_of'] == 'ae1' and table_status['ports']['6']['member_of'] == 'ae1', table_status['ports']['5']
            results['negotiated'] = dict(members=MEMBERS, workers=negotiated['workers']['count'], lanes=len(negotiated['workers']['lanes']))
            # 7. The table cannot take a live member; a lease for an l3 port is refused.
            rc, reply = daemon.table({3: {'mode': 'l3'}, 5: {'mode': 'l3'}, 6: {'mode': 'aggregate', 'group': 'ae1'}})
            assert rc == 0 and reply['ok'] and set(reply['rejected']) == {'5'} and 'ae1' in reply['rejected']['5'], reply
            assert daemon.status()['ports']['5']['desired'] == 'aggregate' and 'ae1' in run('ip', '-n', data_ns, 'link')
            results['rejected_row'] = reply['rejected']['5']
            # 8. Closing the control pipe ends the owner within seconds, gates withdrawn, TAP gone; the daemon stays.
            owner.process.stdin.close(); started = time.monotonic()
            code = owner.wait_exit(10)
            results['pipe_closed'] = dict(exit=code, seconds=round(time.monotonic() - started, 2))
            daemon.wait(lambda: not (rundir / 'ffn-aggregate-ae1-status.json').exists() and not (rundir / 'ffn-aggregate-ae1-intent.json').exists(), 10, 'ae1 withdrawn')
            assert 'ae1' not in run('ip', '-n', data_ns, 'link'), 'TAP survived'
            verbs = [json.loads(l)['verb'] for l in log.read_text().splitlines()]
            assert verbs[-1] == 'network-teardown' and 'network-setup' in verbs and verbs[0] == 'validate', verbs
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and json.loads((directory / 'peer.json').read_text())['distributing']: time.sleep(0.2)
            assert json.loads((directory / 'peer.json').read_text())['distributing'] == [], 'peer still distributing'
            assert daemon.status()['aggregates'] == {} and daemon.status()['ports']['5']['member_of'] is None
            # A lease for an l3 port is refused before anything is held.
            rc, reply = daemon.table({3: {'mode': 'l3'}, 5: {'mode': 'l3'}, 6: {'mode': 'aggregate', 'group': 'ae1'}})
            assert rc == 0 and reply['rejected'] == {}, reply
            daemon.wait(lambda: status_path.exists(), 10, 'port 5 back as l3')
            refused = Owner(env, trunk_ns, str(uuid.uuid4()))
            row = refused.wait_row(lambda r: 'error' in r, 10, heartbeat=False)
            assert 'member 5 is a layer-3 port' in row['error'], row
            refused.wait_exit(5)
            results['lease_refused'] = row['error']
            rc, reply = daemon.table({3: {'mode': 'l3'}, 5: {'mode': 'aggregate', 'group': 'ae1'}, 6: {'mode': 'aggregate', 'group': 'ae1'}})
            daemon.wait(lambda: not status_path.exists(), 10, 'port 5 reserved again')
            # 9. A silent controller: no observation for eight seconds ends the owner with a reason.
            token = str(uuid.uuid4())
            owner = Owner(env, trunk_ns, token)
            owner.wait_row(lambda r: r.get('distributing') == MEMBERS, 30)
            silent = time.monotonic()
            code = owner.wait_exit(14)
            results['silent_controller'] = dict(exit=code, seconds=round(time.monotonic() - silent, 2),
                                                error=next((r['error'] for r in owner.rows if 'error' in r), None))
            assert results['silent_controller']['error'] == 'MP/CP observation lease expired', owner.rows[-2:]
            daemon.wait(lambda: not (rundir / 'ffn-aggregate-ae1-status.json').exists() and 'ae1' not in run('ip', '-n', data_ns, 'link'), 10, 'silent owner withdrawn')
            # 10. The relay terminated: the same cleanup.
            token = str(uuid.uuid4())
            owner = Owner(env, trunk_ns, token)
            owner.wait_row(lambda r: r.get('distributing') == MEMBERS, 30)
            owner.process.send_signal(signal.SIGTERM); started = time.monotonic()
            owner.wait_exit(10)
            daemon.wait(lambda: not (rundir / 'ffn-aggregate-ae1-intent.json').exists() and 'ae1' not in run('ip', '-n', data_ns, 'link'), 10, 'relay-terminated owner withdrawn')
            results['relay_terminated'] = dict(seconds=round(time.monotonic() - started, 2))
            # 11. Control-only: no TAP, no workers, negotiation without collection.
            token = str(uuid.uuid4())
            owner = Owner(env, trunk_ns, token, control_only=True)
            row = owner.wait_row(lambda r: all(m['rx'] == 'current' for m in r.get('members', {}).values()), 30)
            assert row['packet_execution'] == 'control-only' and row['distributing'] == [] and row['workers'] is None, row
            assert 'ae1' not in run('ip', '-n', data_ns, 'link')
            owner.process.stdin.close(); owner.wait_exit(10)
            daemon.wait(lambda: daemon.status()['aggregates'] == {}, 10, 'control-only owner gone')
            results['control_only'] = True
            # 12. A daemon killed outright comes back on its saved table: port 3 re-attached, its stale document replaced.
            old_pid = daemon.process.pid
            daemon.process.send_signal(signal.SIGKILL); daemon.process.wait(timeout=10)
            assert status3.exists() and json.loads(status3.read_text())['pid'] == old_pid
            assert physical_status(3)['running'] is False
            daemon = Daemon(env, trunk_ns, rundir, directory / 'daemon.log')
            daemon.wait(lambda: physical_status(3)['running'] is True and json.loads(status3.read_text())['pid'] == daemon.process.pid, 15, 'port 3 re-attached after restart')
            status = daemon.status()
            assert status['revision'] >= 1 and status['ports']['3']['desired'] == 'l3' and status['ports']['5']['desired'] == 'aggregate', status['ports']['3']
            results['restart'] = dict(old_pid=old_pid, new_pid=daemon.process.pid)
            # 13. The watchdog: a daemon that stops publishing (here: frozen) is retired by `sweep` after
            # eight seconds, the aggregate's lock, TAP and run files reclaimed.
            token = str(uuid.uuid4())
            owner = Owner(env, trunk_ns, token)
            owner.wait_row(lambda r: r.get('distributing') == MEMBERS, 30)
            daemon.process.send_signal(signal.SIGSTOP); frozen = time.monotonic()
            subprocess.run(['ip', 'netns', 'exec', trunk_ns, str(HERE / 'ffn-portd'), 'aggregate', 'sweep'], env=env, check=True, timeout=30)
            assert (rundir / 'ffn-aggregate-ae1-intent.json').exists(), 'a fresh owner was swept'
            time.sleep(9)
            sweep = subprocess.run(['ip', 'netns', 'exec', trunk_ns, str(HERE / 'ffn-portd'), 'aggregate', 'sweep'], env=env,
                                   capture_output=True, text=True, timeout=60)
            assert sweep.returncode == 0, sweep.stderr
            assert daemon.process.poll() is not None, 'frozen daemon survived the sweep'
            assert not (rundir / 'ffn-aggregate-ae1-intent.json').exists() and not (rundir / 'ffn-aggregate-ae1-status.json').exists()
            assert 'ae1' not in run('ip', '-n', data_ns, 'link'), 'TAP survived the sweep'
            owner.wait_exit(10)
            results['sweep'] = dict(seconds=round(time.monotonic() - frozen, 1), stderr=sweep.stderr.strip()[-200:])
            # 14. SIGTERM: every owner withdrawn, status and socket gone, exit 0.
            daemon = Daemon(env, trunk_ns, rundir, directory / 'daemon.log')
            daemon.wait(lambda: physical_status(3)['running'] is True, 15, 'port 3 attached after the sweep')
            started = time.monotonic()
            code = daemon.stop()
            results['sigterm'] = dict(exit=code, seconds=round(time.monotonic() - started, 2))
            assert code == 0 and not status3.exists() and not (rundir / 'ffn-portd.sock').exists() and not (rundir / 'ffn-portd.json').exists()
            assert (rundir / 'ffn-portd-table.json').exists(), 'the table must survive a stop'
            rc, doc = daemon.cli('table', 'status')
            assert rc == 1 and doc.get('daemon') is False, doc
            daemon = None
            return dict(results, physical_ports_used=False, helpers='faked', native_library='real')
        except BaseException:
            # The daemon's own words and the helpers' calls are the diagnosis; the directory is about to go.
            for name in ('daemon.log', 'helpers.log', 'helpers.log.relay', 'peer.log'):
                path = directory / name
                if path.exists(): print('---- ' + name, file=sys.stderr); print(path.read_text()[-4000:], file=sys.stderr)
            raise
        finally:
            if daemon is not None and daemon.process.poll() is None:
                daemon.process.kill(); daemon.process.wait()
            if peer is not None:
                peer.terminate()
                try: peer.wait(timeout=5)
                except subprocess.TimeoutExpired: peer.kill()
            for ns in reversed(made): subprocess.run(['ip', 'netns', 'delete', ns], check=False)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--peer': peer_worker(Path(sys.argv[2]))
    else: print(json.dumps(test(), indent=1))
