#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""ffn-portd against the MP contracts, on a Linux host with root: two disposable
network namespaces, a veth standing in for the OCTEON trunk, the real native packet
library on a real multi-queue TAP, and the Python reference engine as the LACP peer.

Proves, without an appliance: the first status row within the startup budget and
carrying the owner identity; LACPDUs leaving in the trunk's injected-header
envelope and arriving through the OTMH envelope; both members reaching
collection and distribution against the reference engine; and the three
lifetime bindings issue #86 asks for: a closed control pipe, a silent
controller and SIGTERM each end the owner within seconds, with its gates
withdrawn, its TAP torn down and its run files gone.

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
    return dict(group='ae1', token=token, boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                system=SYSTEM, members=list(MEMBERS),
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
    deadline = time.monotonic() + 120
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


class Owner:
    """One ffn-portd aggregate owner under the MP contract."""
    def __init__(self, env, trunk_ns, token, control_only=False):
        self.token = token
        self.process = subprocess.Popen(['ip', 'netns', 'exec', trunk_ns, str(HERE / 'ffn-portd'), 'aggregate', 'serve'],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(env['FAKE_LOG'] + '.stderr', 'a'),
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
                raise RuntimeError('owner exited early: ' + json.dumps(self.rows[-3:]))
            time.sleep(0.05)
        raise RuntimeError('timeout: ' + json.dumps(self.rows[-2:]))

    def wait_exit(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.read()
            if self.process.poll() is not None: return self.process.returncode
            time.sleep(0.05)
        self.process.kill(); raise RuntimeError('owner did not exit within %ss' % seconds)


def test():
    if os.geteuid() != 0: raise RuntimeError('root required for isolated namespaces')
    if not (HERE / 'ffn-portd').exists() or not (HERE / 'libffn-packet.so').exists():
        raise RuntimeError('build ffn-portd and libffn-packet.so first')
    # The native library bounds names at IFNAMSIZ; the namespace name is one of them.
    tag = str(os.getpid() % 10000)
    trunk_ns, data_ns = 'agt' + tag, 'agd' + tag
    made = []; peer = None; results = {}
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
                   LD_LIBRARY_PATH=str(HERE), FAKE_LOG=str(log), FAKE_NS=data_ns)
        try:
            for ns in (trunk_ns, data_ns):
                run('ip', 'netns', 'add', ns); made.append(ns); run('ip', '-n', ns, 'link', 'set', 'lo', 'up')
            run('ip', '-n', trunk_ns, 'link', 'add', 'ffnpkt0', 'type', 'veth', 'peer', 'name', 'peer')
            for name in ('ffnpkt0', 'peer'): run('ip', '-n', trunk_ns, 'link', 'set', name, 'mtu', '1600', 'up')
            peer = subprocess.Popen(['ip', 'netns', 'exec', trunk_ns, sys.executable, str(Path(__file__).resolve()), '--peer', str(directory)],
                                    stdout=open(directory / 'peer.log', 'w'), stderr=subprocess.STDOUT)
            # 1. A full owner negotiates both members against the reference engine.
            token = str(uuid.uuid4())
            owner = Owner(env, trunk_ns, token)
            first = owner.wait_row(lambda r: 'token' in r, 30)
            assert 'error' not in first, first
            assert first['token'] == token and first['boot_id'] == intent(token)['boot_id'], first
            assert first['packet_execution'] == 'native-c' and first['owner'] == 'ffn-portd', first
            assert first['attachment_ready'] is True and first['configuration_ready'] is True, first
            import hashlib
            expected = hashlib.sha256(json.dumps({k: intent(token)[k] for k in ('network', 'lldp')}, sort_keys=True).encode()).hexdigest()
            assert first['configuration_revision'] == expected, first['configuration_revision']
            negotiated = owner.wait_row(lambda r: r.get('distributing') == MEMBERS, 25)
            peer_status = json.loads((directory / 'peer.json').read_text())
            assert peer_status['distributing'] == MEMBERS, peer_status
            assert all(m['rx'] == 'current' and m['matched'] for m in negotiated['members'].values()), negotiated['members']
            assert (rundir / 'ffn-aggregate-ae1-status.json').exists() and (rundir / 'ffn-aggregate-ae1-intent.json').exists()
            assert 'ae1' in run('ip', '-n', data_ns, 'link')
            results['negotiated'] = dict(members=MEMBERS, workers=negotiated['workers']['count'], lanes=len(negotiated['workers']['lanes']))
            # 2. Closing the control pipe ends the owner within seconds, gates withdrawn, TAP gone.
            owner.process.stdin.close(); started = time.monotonic()
            code = owner.wait_exit(10)
            results['pipe_closed'] = dict(exit=code, seconds=round(time.monotonic() - started, 2))
            assert not (rundir / 'ffn-aggregate-ae1-status.json').exists(), 'status file survived'
            assert not (rundir / 'ffn-aggregate-ae1-intent.json').exists(), 'intent file survived'
            assert 'ae1' not in run('ip', '-n', data_ns, 'link'), 'TAP survived'
            verbs = [json.loads(l)['verb'] for l in log.read_text().splitlines()]
            assert verbs[-1] == 'network-teardown' and 'network-setup' in verbs and verbs[0] == 'validate', verbs
            # The peer must see the withdrawal: our members stop advertising.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and json.loads((directory / 'peer.json').read_text())['distributing']: time.sleep(0.2)
            assert json.loads((directory / 'peer.json').read_text())['distributing'] == [], 'peer still distributing'
            # 3. A silent controller: no observation for eight seconds ends the owner with a reason.
            token = str(uuid.uuid4())
            owner = Owner(env, trunk_ns, token)
            owner.wait_row(lambda r: r.get('distributing') == MEMBERS, 30)
            silent = time.monotonic()
            code = owner.wait_exit(14)
            results['silent_controller'] = dict(exit=code, seconds=round(time.monotonic() - silent, 2),
                                                error=next((r['error'] for r in owner.rows if 'error' in r), None))
            assert results['silent_controller']['error'] == 'MP/CP observation lease expired', owner.rows[-2:]
            assert not (rundir / 'ffn-aggregate-ae1-status.json').exists() and 'ae1' not in run('ip', '-n', data_ns, 'link')
            # 4. SIGTERM: a clean stop, exit 0, same cleanup.
            token = str(uuid.uuid4())
            owner = Owner(env, trunk_ns, token)
            owner.wait_row(lambda r: r.get('distributing') == MEMBERS, 30)
            owner.process.send_signal(signal.SIGTERM); started = time.monotonic()
            code = owner.wait_exit(10)
            results['sigterm'] = dict(exit=code, seconds=round(time.monotonic() - started, 2))
            assert code == 0, code
            assert not (rundir / 'ffn-aggregate-ae1-intent.json').exists() and 'ae1' not in run('ip', '-n', data_ns, 'link')
            # 5. Control-only: no TAP, no workers, negotiation without collection.
            token = str(uuid.uuid4())
            owner = Owner(env, trunk_ns, token, control_only=True)
            row = owner.wait_row(lambda r: all(m['rx'] == 'current' for m in r.get('members', {}).values()), 30)
            assert row['packet_execution'] == 'control-only' and row['distributing'] == [] and row['workers'] is None, row
            assert 'ae1' not in run('ip', '-n', data_ns, 'link')
            owner.process.stdin.close(); owner.wait_exit(10)
            results['control_only'] = True
            # 6. The watchdog: an owner that stops writing status (here: frozen) is retired by `sweep`
            # after eight seconds, its lock, TAP and run files reclaimed, even though it still holds
            # the lock and ignores SIGTERM while stopped.
            token = str(uuid.uuid4())
            owner = Owner(env, trunk_ns, token)
            owner.wait_row(lambda r: r.get('distributing') == MEMBERS, 30)
            owner.process.send_signal(signal.SIGSTOP); frozen = time.monotonic()
            subprocess.run(['ip', 'netns', 'exec', trunk_ns, str(HERE / 'ffn-portd'), 'aggregate', 'sweep'], env=env, check=True, timeout=30)
            assert (rundir / 'ffn-aggregate-ae1-intent.json').exists(), 'a fresh owner was swept'
            time.sleep(9)
            sweep = subprocess.run(['ip', 'netns', 'exec', trunk_ns, str(HERE / 'ffn-portd'), 'aggregate', 'sweep'], env=env,
                                   capture_output=True, text=True, timeout=60)
            assert sweep.returncode == 0, sweep.stderr
            assert owner.process.poll() is not None, 'frozen owner survived the sweep'
            assert not (rundir / 'ffn-aggregate-ae1-intent.json').exists() and not (rundir / 'ffn-aggregate-ae1-status.json').exists()
            assert 'ae1' not in run('ip', '-n', data_ns, 'link'), 'TAP survived the sweep'
            results['sweep'] = dict(seconds=round(time.monotonic() - frozen, 1), stderr=sweep.stderr.strip()[-200:])
            # 7. A physical (optical) owner: the TAP the network owner made, the intent the MP's
            # start wrote, the status document the policy bindings read, carrier always on.
            boot = intent(token)['boot_id']
            def physical(port, source):
                run('ip', '-n', data_ns, 'tuntap', 'add', 'dev', 'p%d' % port, 'mode', 'tap', 'multi_queue')
                run('ip', '-n', data_ns, 'link', 'set', 'p%d' % port, 'up')
                (rundir / ('ffn-physical-%d-intent.json' % port)).write_text(json.dumps(dict(boot_id=boot, front={str(port): source})))
                process = subprocess.Popen(['ip', 'netns', 'exec', trunk_ns, str(HERE / 'ffn-portd'), 'physical', 'serve', '--port', str(port)],
                                           stdout=open(env['FAKE_LOG'] + '.physical', 'a'), stderr=subprocess.STDOUT, env=env)
                status_path = rundir / ('ffn-physical-%d-status.json' % port)
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and not status_path.exists():
                    if process.poll() is not None: raise RuntimeError('physical owner exited: ' + open(env['FAKE_LOG'] + '.physical').read()[-1500:])
                    time.sleep(0.1)
                assert status_path.exists(), 'physical owner published nothing'
                return process, status_path
            def carrier(port):
                link = json.loads(run('ip', '-n', data_ns, '-d', '-j', 'link', 'show', 'dev', 'p%d' % port))[0]
                return 'NO-CARRIER' not in link.get('flags', [])
            def status(port):
                out = subprocess.run(['ip', 'netns', 'exec', trunk_ns, str(HERE / 'ffn-portd'), 'physical', 'status', '--port', str(port)], env=env,
                                     capture_output=True, text=True, timeout=20)
                return json.loads(out.stdout)
            process, status_path = physical(5, 16)
            time.sleep(1.5)
            row = json.loads(status_path.read_text())
            assert row['owner'] == 'physical-5' and row['ports'] == [5] and row['packet_execution'] == 'native-c', row
            link = json.loads(run('ip', '-n', data_ns, '-j', 'link', 'show', 'dev', 'p5'))[0]
            assert row['interfaces'] == {'p5': link['ifindex']}, (row['interfaces'], link['ifindex'])
            assert row['carrier']['policy'] == 'always' and row['workers']['count'] == 2 and 'rx_p5' in row['counters'], row
            assert status(5)['running'] is True and carrier(5)
            process.send_signal(signal.SIGTERM); started = time.monotonic()
            code = process.wait(timeout=10)
            results['physical'] = dict(exit=code, seconds=round(time.monotonic() - started, 2), ifindex=link['ifindex'])
            assert code == 0 and not status_path.exists() and status(5)['running'] is False
            # 8. A copper port: carrier follows the delivered faceplate observation under its lease.
            process, status_path = physical(3, 14)
            time.sleep(1.5)
            row = json.loads(status_path.read_text())
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
                return subprocess.run(['ip', 'netns', 'exec', trunk_ns, str(HERE / 'ffn-portd'), 'links'], env=env, input=json.dumps(payload),
                                      capture_output=True, text=True, timeout=20)
            assert deliver(True).returncode == 0
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and not carrier(3): time.sleep(0.2)
            assert carrier(3), 'carrier did not follow the link-up observation'
            assert json.loads(status_path.read_text())['carrier']['up'] is True
            stale = deliver(True, seq=sequence - 1)
            assert stale.returncode != 0 and 'stale' in stale.stdout, stale.stdout
            assert deliver(True, speed=100).returncode == 0   # PHY and MAC speeds disagree: not ready
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and carrier(3): time.sleep(0.2)
            assert not carrier(3), 'carrier survived a speed mismatch'
            assert 'speeds' in json.loads(status_path.read_text())['carrier']['reason']
            assert deliver(True).returncode == 0
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and not carrier(3): time.sleep(0.2)
            assert carrier(3)
            # No further delivery: the four-second lease lapses and carrier drops.
            lapsed = time.monotonic()
            deadline = time.monotonic() + 9
            while time.monotonic() < deadline and carrier(3): time.sleep(0.2)
            assert not carrier(3), 'carrier outlived its lease'
            results['copper_carrier'] = dict(lease_lapse_seconds=round(time.monotonic() - lapsed, 1), reason=json.loads(status_path.read_text())['carrier']['reason'])
            process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=10) == 0 and not status_path.exists()
            return dict(results, physical_ports_used=False, helpers='faked', native_library='real')
        except BaseException:
            # The daemon's own words and the helpers' calls are the diagnosis; the directory is about to go.
            for name in ('helpers.log', 'helpers.log.stderr', 'peer.log'):
                path = directory / name
                if path.exists(): print('---- ' + name, file=sys.stderr); print(path.read_text()[-4000:], file=sys.stderr)
            raise
        finally:
            if peer is not None:
                peer.terminate()
                try: peer.wait(timeout=5)
                except subprocess.TimeoutExpired: peer.kill()
            for ns in reversed(made): subprocess.run(['ip', 'netns', 'delete', ns], check=False)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--peer': peer_worker(Path(sys.argv[2]))
    else: print(json.dumps(test(), indent=1))
