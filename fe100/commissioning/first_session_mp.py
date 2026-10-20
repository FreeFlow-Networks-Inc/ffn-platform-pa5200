#!/usr/bin/env python3
"""MP orchestrator: one real LAN->WAN session through the FE100, measured, then unwound.

  first_session_mp.py --flow-host 10.1.0.63 --target 151.101.2.132 [--window 60] [--exclude-ct N]

Ingress uses the qualification path: the DP diverts the flow's frames into the lab port's MAC loop
(front 13 / switch port 7), which the lab recipe force-forwards to the FE100 link (port 3), so the
FE100 sees them exactly as the qualification probes. The FE100 does the lookup, NAT, MAC/VLAN rewrite
and egress selection; its output reaches the real WAN and LAN ports through the port-3 egress rules.
Everything is journaled and undone in reverse order. The flow itself is driven from outside.
"""
import argparse
import os
import json
import select
import subprocess
import sys
import time
from pathlib import Path

LOG = Path('/var/log/ffn-fe100-first-session-' + str(time.time_ns()) + '.json')
report = {'schema': 2, 'steps': [], 'cleanup_errors': []}
LD = 'LD_LIBRARY_PATH=/usr/local/lib64:/usr/local/lib64/3p:/usr/local/lib/ffn/owner-deps LD_PRELOAD=/usr/lib/mips64-linux-gnuabi64/libsqlite3.so.0'
# Where the helpers are staged on each plane (copy steer_cp.py + first_session_cp.py to the CP,
# steer_dp.py + ct_liberal.py to the DP, ffn_dp_ssh.py here); the same path on all three.
STAGE = os.environ.get('FFN_COMMISSIONING_STAGE', '/tmp')
LAB_FRONT, LAB_PORT = 13, 7


def save():
    LOG.write_text(json.dumps(report, indent=1, default=str))


def step(name, **data):
    report['steps'].append(dict(name=name, at=time.time(), **data)); save()
    print(json.dumps(dict(step=name, **data), default=str)[:700], flush=True)


def sh(argv, inp=None, timeout=60):
    r = subprocess.run(argv, input=inp, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def cp_json(command, request=None, timeout=90):
    rc, out, err = sh(['/usr/local/sbin/ffn-cp', command], json.dumps(request or {}), timeout)
    lines = [l for l in out.splitlines() if l.startswith('{')]
    if rc or not lines:
        raise RuntimeError('CP command failed: %s %s' % (out[-600:], err[-600:]))
    return json.loads(lines[-1])


def cp_op(op, request=None, timeout=90):
    return cp_json('cd /; python3 ' + STAGE + '/steer_cp.py ' + op, request, timeout)


def bcm_lab(mode, **extra):
    return cp_json('cd /; python3 /usr/local/sbin/ffn_fe100_bcm_lab.py', dict(mode=mode, **extra))


def dp(command, inp=None, timeout=60):
    return sh(['python3', STAGE + '/ffn_dp_ssh.py', 'bash', '-c', command], inp, timeout)


def inventory_row(host, dport, exclude_ct=None):
    hits = []
    for i in range(200):
        try:
            table = json.load(open('/run/ffn-fe100-session-inventory.json')); rows = table['sessions']
            if i % 15 == 0: step('inventory-wait', ready=table.get('ready'), rows=len(rows), reason=table.get('reason'))
        except Exception:
            rows = []
        hits = [r for r in rows if r.get('src') == host and r.get('dport') == dport and r.get('proto') == 6 and r.get('ct') not in (exclude_ct or ())]
        ready = [r for r in hits if r.get('candidate') and (r.get('l3') or {}).get('available') and r.get('nat', {}).get('source')]
        if ready:
            return max(ready, key=lambda r: r.get('since') or 0)
        time.sleep(2)
    raise RuntimeError('the test flow never became an L3-ready candidate: ' + json.dumps(hits[:1]))


def conntrack_counters(row):
    rc, out, err = dp('ip netns exec ffn-data conntrack -L -p tcp --orig-src %s --orig-dst %s --orig-port-src %d --orig-port-dst %d -o extended 2>/dev/null'
                      % (row['src'], row['dst'], row['sport'], row['dport']))
    packets = [int(t.split('=')[1]) for t in out.split() if t.startswith('packets=')]
    return dict(packets=packets, line=out.strip()[:200])


import socket
import struct


class CpuCapture:
    """FE100 CPU messages forced to the MP fabric NIC: 32-byte header (type at byte 3, code at 23) then the packet."""
    def __init__(self, dev='enp8s0f1', addresses=()):
        self.dev, self.addresses = dev, [socket.inet_aton(x) for x in addresses]
        self.link_raised = False; self.feature = False; self.sock = None; self.frames = []
        link = json.loads(subprocess.check_output(['ip', '-j', 'link', 'show', 'dev', dev], text=True))[0]
        if 'UP' not in link['flags']:
            subprocess.run(['ip', 'link', 'set', 'dev', dev, 'up'], check=True); self.link_raised = True
        if 'rx-all: off' in subprocess.check_output(['ethtool', '-k', dev], text=True):
            subprocess.run(['ethtool', '-K', dev, 'rx-all', 'on'], check=True); self.feature = True
        self.sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
        self.sock.setsockopt(socket.SOL_SOCKET, 33, 8 * 1024 * 1024)
        self.sock.bind((dev, 0)); self.sock.setblocking(False)
        self.sock.setsockopt(263, 1, struct.pack('IHH8s', socket.if_nametoindex(dev), 1, 0, bytes(8)))

    def poll(self):
        while select.select([self.sock], [], [], 0)[0]:
            raw, addr = self.sock.recvfrom(65536)
            if addr[2] == socket.PACKET_OUTGOING or not any(x in raw for x in self.addresses):
                continue
            item = dict(length=len(raw), head=raw[:64].hex())
            if len(raw) >= 36:
                # retained ITMH (4) + common header (16: type at its byte 3) + message info (8: flow id, code) + packet info (8)
                item.update(type=raw[7], code=raw[27], flow_id=int.from_bytes(raw[20:24], 'big'), ethernet=raw[36:50].hex())
            self.frames.append(item)
        return self.frames

    def close(self):
        errors = []
        try: self.sock.close()
        except Exception as e: errors.append(str(e))
        for enabled, argv in ((self.feature, ['ethtool', '-K', self.dev, 'rx-all', 'off']), (self.link_raised, ['ip', 'link', 'set', 'dev', self.dev, 'down'])):
            if enabled:
                try: subprocess.run(argv, check=True, capture_output=True)
                except Exception as e: errors.append(str(e))
        return errors


def delta(a, b):
    def short(k):
        for tail in ('_stats_ctr_no_rd_clr', '_sta_ctr_no_rd_clr', '_ctr_no_rd_clr'):
            if k.endswith(tail): return k[:-len(tail)]
        return k
    return {short(k): b[k] - a[k] for k in b if k in a and b[k] != a[k]}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--flow-host', default='10.1.0.63'); p.add_argument('--target', required=True)
    p.add_argument('--window', type=int, default=60)
    p.add_argument('--exclude-ct', type=int, action='append', default=[])
    a = p.parse_args()
    neighbours = {}
    mac = None; baseline_started = False; path_enabled = False
    controller = None; egress_rules = []; diverter = None; capture = None; cpucap = None
    try:
        rc, out, err = dp('ip -n ffn-data neigh show %s; ip -n ffn-data neigh show 152.193.185.9; ip -n ffn-data -br link show ae1.69; ip -n ffn-data -br link show p5' % a.flow_host)
        for line in out.splitlines():
            parts = line.split()
            if 'lladdr' in parts:
                neighbours[parts[0]] = parts[parts.index('lladdr') + 1]
            elif parts and parts[0] in ('ae1.69@ae1', 'p5'):
                neighbours[parts[0].split('@')[0]] = parts[2]
        step('facts', neighbours=neighbours)
        if a.flow_host not in neighbours or '152.193.185.9' not in neighbours:
            raise RuntimeError('next-hop MACs unresolved')
        row = inventory_row(a.flow_host, 80, a.exclude_ct)
        step('session', row={k: row.get(k) for k in ('id', 'ct', 'src', 'sport', 'dst', 'dport', 'xsrc', 'xsport', 'nat', 'rule', 'vsys', 'state_name', 'l3', 'since')})
        original = dict(source=row['src'], destination=row['dst'], source_port=row['sport'], destination_port=row['dport'], protocol=6)
        reply = dict(source=row['xdst'], destination=row['xsrc'], source_port=row['xdport'], destination_port=row['xsport'], protocol=6)
        plan = dict(ingress_pport=LAB_FRONT, mtu=9216, miss_sysport=8,
                    lan=dict(vlan=69, zone=4094, logical=21, physical=32, smac=neighbours['ae1.69']),
                    wan=dict(vlan=0, zone=4093, logical=5, physical=16, smac=neighbours['p5']),
                    queues={'16': 36, '32': 20},
                    session=dict(id=0x3000, original=original, reply=reply),
                    forward_dmac=neighbours['152.193.185.9'], reverse_dmac=neighbours[a.flow_host],
                    # LIF 1 is front 13's baseline selector (any VLAN); it is journaled and restored.
                    pools=dict(nexthop=[20, 21, 22], smac=[20, 21], lif=[20, 1], lef=[20, 21], qm=[20, 21], acl=[24, 25],
                               txport=[5, 21], rxport=[LAB_FRONT]), rxport_physical={str(LAB_FRONT): LAB_PORT})
        step('plan', plan=plan)
        # 1. the lab ingress: MAC loop on front 13, baseline route 7->24, then the session path 7->3
        mac = subprocess.Popen(['/usr/local/sbin/ffn-cp', 'cd /; python3 /usr/local/sbin/ffn_fe100_mac_lab.py --serve --port 13'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if not select.select([mac.stdout], [], [], 60)[0]: raise RuntimeError('MAC lab timed out')
        ready = json.loads(mac.stdout.readline()); step('mac-loop', ready=ready.get('ready'))
        if ready.get('ready') is not True: raise RuntimeError('MAC loop not ready')
        step('baseline-begin', result=bcm_lab('baseline-begin', ports=[LAB_PORT]).get('baseline')); baseline_started = True
        r = bcm_lab('session-path-enable'); path_enabled = True
        step('session-path-enable', markers=[m for m in r.get('markers', []) if 'ROUTE' in m or 'FAIL' in m])
        # 2. the FE100 tables for this one session
        controller = subprocess.Popen(['/usr/local/sbin/ffn-cp', 'cd /; env ' + LD + ' python3 ' + STAGE + '/first_session_cp.py --serve'],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cp_cmd(message, timeout=120):
            controller.stdin.write(json.dumps(message) + '\n'); controller.stdin.flush()
            if not select.select([controller.stdout], [], [], timeout)[0]: raise TimeoutError('controller timeout')
            line = controller.stdout.readline()
            if not line: raise RuntimeError('controller exited: ' + controller.stderr.read()[-2000:])
            return json.loads(line)
        if not select.select([controller.stdout], [], [], 90)[0]: raise RuntimeError('controller did not start')
        ready = json.loads(controller.stdout.readline()); step('controller', ready=ready)
        if ready.get('ready') is not True: raise RuntimeError('controller not ready')
        r = cp_cmd(dict(op='prepare', plan=plan)); step('prepare', ok=r.get('ok'), error=r.get('error'))
        if not r.get('ok'): raise RuntimeError('prepare failed')
        r = cp_cmd(dict(op='install')); step('install', ok=r.get('ok'), error=r.get('error'))
        if not r.get('ok'): raise RuntimeError('install failed')
        # 3. FE100 -> front ports: the lab-proven port-3 egress rules keyed on the DSA destination and the real next-hop MAC
        for destination, dmac, prio in ((16, neighbours['152.193.185.9'], 23), (32, neighbours[a.flow_host], 24)):
            rule = cp_op('egress-create', dict(destination=destination, dmac=dmac, priority=prio))
            step('egress-rule', destination=destination, ids=rule['ids'])
            if rule['ids'].get('rv') != 0: raise RuntimeError('egress rule failed')
            egress_rules.append(rule['ids'])
        # 3b. FE100 CPU messages (exceptions, misses) to the MP capture port, as the lab's IL capture does
        cpucap = cp_op('cpu-capture-begin'); step('cpu-capture-begin', previous_header=cpucap.get('previous_header'), markers=cpucap['markers'][-2:])
        if cpucap.get('previous_header') is None or not any(m == 'FFN_DONE' for m in cpucap['markers']):
            raise RuntimeError('port 20 capture setup failed')
        capture = CpuCapture(addresses=(row['src'], row['dst'], row['xsrc']))
        # 4. divert the flow on the DP into the lab loop
        before_ct = conntrack_counters(row); s0 = cp_cmd(dict(op='snapshot'))['registers']
        args = ['--lan-dev', 'ae1.69', '--lan-vlan', '69', '--wan-dev', 'p5', '--proto', '6', '--src', row['src'], '--sport', str(row['sport']),
                '--dst', row['dst'], '--dport', str(row['dport']), '--xsrc', row['xsrc'], '--xsport', str(row['xsport']),
                '--seconds', str(a.window + 30), '--inject-port', str(LAB_FRONT)]
        diverter = subprocess.Popen(['python3', STAGE + '/ffn_dp_ssh.py', 'python3', STAGE + '/steer_dp.py'] + args,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if not select.select([diverter.stdout], [], [], 30)[0]: raise RuntimeError('diverter did not start')
        step('divert', first=diverter.stdout.readline().strip()[:300], conntrack_before=before_ct['packets'])
        # the kernel flowtable's hand-back fixup: window tracking liberal in both directions, timeout refreshed,
        # so the session survives the DP not seeing its packets while it is in hardware (run 7 stalled without this)
        rc, out, err = dp('ip netns exec ffn-data python3 ' + STAGE + '/ct_liberal.py %s %d %s %d --timeout 432000'
                          % (row['src'], row['sport'], row['dst'], row['dport']))
        step('ct-liberal', rc=rc, out=out.strip()[:200], err=err.strip()[-200:])
        if rc: raise RuntimeError('conntrack fixup failed: ' + err.strip()[-300:])
        samples = []
        for i in range(a.window // 10):
            time.sleep(10)
            try:
                mac.stdin.write('{"op":"status"}\n'); mac.stdin.flush()
                if select.select([mac.stdout], [], [], 20)[0]: mac.stdout.readline()
            except Exception as error:
                report['cleanup_errors'].append('mac heartbeat: ' + str(error))
            s1 = cp_cmd(dict(op='snapshot'))['registers']
            ct = conntrack_counters(row)
            while select.select([diverter.stdout], [], [], 0)[0]:
                line = diverter.stdout.readline()
                if line.startswith('{"event": "counts"'): report['divert_counts'] = json.loads(line)
            frames = capture.poll()
            codes = {}
            for f in frames:
                key = '%s/%s' % (f.get('type'), f.get('code')); codes[key] = codes.get(key, 0) + 1
            sample = dict(t=(i + 1) * 10, fe100=delta(s0, s1), conntrack=ct['packets'], divert=report.get('divert_counts'),
                          cpu_messages=len(frames), cpu_codes=codes)
            samples.append(sample); step('sample', **sample)
        report['hardware_window'] = samples; report['cpu_frames'] = capture.frames[:40]; save()
        step('cpu-frames', frames=[dict(type=f.get('type'), code=f.get('code'), flow_id=f.get('flow_id'), head=f['head'][:80]) for f in capture.frames[:6]])
        # 5. stop diverting, watch the software path resume
        diverter.terminate()
        try: out, err = diverter.communicate(timeout=20)
        except subprocess.TimeoutExpired: diverter.kill(); out, err = diverter.communicate()
        stopped = [l for l in out.splitlines() if l.startswith('{"event": "stopped"')]
        step('divert-stopped', last=(stopped[-1] if stopped else out[-300:]), err=err[-300:]); diverter = None
        time.sleep(15)
        s2 = cp_cmd(dict(op='snapshot'))['registers']
        step('software-resumed', conntrack=conntrack_counters(row)['packets'], fe100_since_stop=delta(s1, s2))
    except BaseException as error:
        report['error'] = str(error); save(); raise
    finally:
        if capture is not None:
            errs = capture.close()
            if errs: report['cleanup_errors'].append('capture: ' + '; '.join(errs))
        if cpucap is not None and cpucap.get('previous_header') is not None:
            try: step('cpu-capture-end', markers=cp_op('cpu-capture-end', dict(header=cpucap['previous_header']))['markers'][-2:])
            except Exception as error: report['cleanup_errors'].append('cpu-capture-end: ' + str(error))
        if diverter is not None and diverter.poll() is None:
            diverter.terminate()
            try: diverter.communicate(timeout=20)
            except Exception: diverter.kill()
        for ids in reversed(egress_rules):
            try: step('egress-rule-delete', ids=ids, result=cp_op('rule-delete', ids)['markers'][-2:])
            except Exception as error: report['cleanup_errors'].append('rule-delete: ' + str(error))
        if controller is not None:
            try:
                if controller.poll() is None:
                    controller.stdin.write('{"op":"remove"}\n'); controller.stdin.flush()
                    if select.select([controller.stdout], [], [], 120)[0]: step('remove', reply=controller.stdout.readline().strip()[:160])
                    controller.stdin.write('{"op":"finish"}\n'); controller.stdin.flush()
                    if select.select([controller.stdout], [], [], 180)[0]: step('restore', reply=controller.stdout.readline().strip()[:300])
                controller.wait(timeout=60)
            except Exception as error:
                report['cleanup_errors'].append('controller: ' + str(error))
        if path_enabled:
            try: step('session-path-restore', ok=bcm_lab('session-path-restore').get('completed'))
            except Exception as error: report['cleanup_errors'].append('session-path-restore: ' + str(error))
        if baseline_started:
            try: step('baseline-end', restored=bcm_lab('baseline-end').get('restored'))
            except Exception as error: report['cleanup_errors'].append('baseline-end: ' + str(error))
        if mac is not None:
            try:
                mac.stdin.write('{"op":"finish"}\n'); mac.stdin.flush()
                if select.select([mac.stdout], [], [], 40)[0]: step('mac-restore', result=json.loads(mac.stdout.readline() or '{}'))
                mac.wait(timeout=30)
            except Exception as error:
                report['cleanup_errors'].append('mac restore: ' + str(error))
        save()
        print(json.dumps(dict(report=str(LOG), error=report.get('error'), cleanup_errors=report['cleanup_errors'])), flush=True)


if __name__ == '__main__':
    main()
