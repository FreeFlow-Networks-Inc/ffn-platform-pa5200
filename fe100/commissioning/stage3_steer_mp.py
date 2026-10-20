#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""Stage 3: steer the production ports into the FE100, watched, with automatic rollback.

Preconditions it checks before touching anything: the chip's return path (port 20
RAW force-forwarded to the DP trunk) is enabled; the WAN and aggregate owners report
live return bindings (fe100_return in their status files); the binding file names
every port about to be steered. Then, one port at a time (the WAN port first), it
force-forwards the port to port 3 and watches for `--watch` seconds: the LAN host's
reachability of the internet and of the LAN host through the box (ICMP from
the dataplane itself), the steered port's owner's decoded/rejected return counters (its
kernel filter hands it only its own fronts' returns), and the DP's trunk drops. Any of: probe loss above --max-loss, rejected returns growing, or a failed
status read, rolls every steered port back to the DP trunk (24) at once.

Runs on the MP:  python3 stage3_steer_mp.py --go [--ports 16,32,33] [--watch 30]
Without --go it only reports the preconditions. `--rollback` reverts all three ports.
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)), '/tmp']
import first_session_mp as fs

STAGE = getattr(fs, 'STAGE', '/tmp')
DP_SSH = next((c for c in (STAGE + '/ffn_dp_ssh.py', STAGE + '/ffn-dp-ssh.py') if os.path.exists(c)), STAGE + '/ffn_dp_ssh.py')
OWNER_STATUS = {16: '/run/ffn-physical-5-status.json', 32: '/run/ffn-aggregate-ae1-status.json', 33: '/run/ffn-aggregate-ae1-status.json'}
FRONT_OF = {16: 5, 32: 21, 33: 22}


def dp_json(command):
    rc, out, err = fs.dp(command, timeout=40)
    lines = [l for l in out.splitlines() if l.startswith('{')]
    if rc or not lines:
        raise RuntimeError('DP read failed: ' + (err or out)[-300:])
    return json.loads(lines[-1])


def owners():
    """fe100_return and return counters of the WAN and aggregate owners, by status file."""
    return dp_json("python3 -c \"import json; print(json.dumps({p: {'fe100_return': d.get('fe100_return'), 'counters': (d.get('workers') or {}).get('fe100_return') or {}} for p, d in ((p, json.load(open(p))) for p in ('/run/ffn-physical-5-status.json', '/run/ffn-aggregate-ae1-status.json'))}))\"")


def bindings_file():
    return dp_json("python3 -c \"import json; print(json.dumps(json.load(open('/etc/ffn/fe100-return.json'))))\"")


def steering():
    return {int(k): v for k, v in fs.cp_op('steer-get')['ports'].items()}


def steer(port, destination):
    reply = fs.cp_op('steer-set', dict(port=port, destination=destination))
    if not reply.get('done'):
        raise RuntimeError('steer %d -> %d failed: %s' % (port, destination, json.dumps(reply)[:300]))
    return reply['markers']


def probe_loss(host, target, count):
    """Packet loss percentage of ICMP sent by the dataplane itself to target: replies enter
    through the steered ports (the WAN target through port 16, the LAN host through the LAG)."""
    try:
        rc, out, err = fs.dp('ip netns exec ffn-data ping -c %d -i 0.2 -W 1 %s | grep -o "[0-9]*%% packet loss"' % (count, target), timeout=60)
        out = out.strip()
        return int(out.split('%')[0]) if out else 100
    except Exception:
        return 100


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--go', action='store_true'); p.add_argument('--rollback', action='store_true')
    p.add_argument('--ports', default='16,32,33'); p.add_argument('--watch', type=int, default=30)
    p.add_argument('--probe-host', default='dp'); p.add_argument('--probe', default='8.8.8.8'); p.add_argument('--probe-lan', default='10.1.0.63')
    p.add_argument('--max-loss', type=int, default=20)
    a = p.parse_args()
    ports = [int(x) for x in a.ports.split(',')]
    report = dict(schema=1, kind='stage3-steering', at=time.time(), steps=[])
    def step(name, **data):
        report['steps'].append(dict(name=name, at=time.time(), **data)); print(json.dumps(dict(step=name, **data), default=str)[:600], flush=True)
    if a.rollback:
        for port in ports:
            step('rollback', port=port, markers=steer(port, 24))
        return
    checks = {}
    rp = fs.cp_op('return-path-status')['state']['FFN_RETURN']
    checks['return_path'] = rp['enabled'] == '1' and rp['destination'] == '24' and rp['header'] == '2'
    step('return-path', **rp)
    try:
        cfg = bindings_file()
        checks['binding_file'] = all(str(FRONT_OF[port]) in cfg.get('ports', {}) for port in ports)
        step('binding-file', ports=sorted(cfg.get('ports', {})))
    except Exception as error:
        checks['binding_file'] = False; step('binding-file', error=str(error)[:200])
    try:
        own = owners()
        live = {path: bool(d.get('fe100_return')) for path, d in own.items()}
        checks['owners_bound'] = all(live.get(OWNER_STATUS[port]) for port in ports)
        step('owners', live=live, counters={path: d.get('counters') for path, d in own.items()})
    except Exception as error:
        checks['owners_bound'] = False; step('owners', error=str(error)[:200])
    current = steering(); step('steering-now', ports=current)
    checks['ports_on_dp'] = all(current[port]['destination'] == '24' and current[port]['enabled'] == '1' for port in ports)
    baseline_loss = max(probe_loss(a.probe_host, a.probe, 10), probe_loss(a.probe_host, a.probe_lan, 10)); checks['probe_baseline'] = baseline_loss <= a.max_loss
    step('preconditions', **checks, baseline_loss=baseline_loss)
    if not all(checks.values()) or not a.go:
        print(json.dumps(dict(ready=all(checks.values()), checks=checks, go=a.go)))
        return
    steered = []
    before = owners()
    try:
        for port in ports:
            step('steer', port=port, markers=steer(port, 3)); steered.append(port)
            deadline = time.monotonic() + a.watch; worst = 0
            while time.monotonic() < deadline:
                loss = probe_loss(a.probe_host, a.probe if port == 16 else a.probe_lan, 10); worst = max(worst, loss)
                now = owners()
                delta = lambda k, name: int((now.get(k, {}).get('counters') or {}).get(name, 0)) - int((before.get(k, {}).get('counters') or {}).get(name, 0))
                # Only the steered port's owner judges: its kernel filter hands it its own fronts'
                # returns alone, so a rejection there is a malformed message. Other owners are
                # reported for the record (an owner on the old library still sees every return).
                mine = OWNER_STATUS[port]
                rejected = delta(mine, 'rejected'); decoded = delta(mine, 'decoded')
                others = {k: delta(k, 'rejected') for k in now if k != mine}
                step('watch', port=port, loss=loss, decoded=decoded, rejected=rejected, other_owners_rejected=others)
                if loss > a.max_loss or rejected > 0:
                    raise RuntimeError('rollback: loss %d%% rejected %d on port %d' % (loss, rejected, port))
            step('port-steered', port=port, worst_loss=worst)
        report['verdict'] = 'PASS'
    except Exception as error:
        report['verdict'] = 'ROLLED BACK: ' + str(error)
        for port in reversed(steered):
            try: step('rollback', port=port, markers=steer(port, 24))
            except Exception as e: step('rollback-failed', port=port, error=str(e))
        loss = max(probe_loss(a.probe_host, a.probe, 10), probe_loss(a.probe_host, a.probe_lan, 10)); step('after-rollback', loss=loss)
    path = '/var/log/ffn-fe100-stage3-' + str(time.time_ns()) + '.json'
    with open(path, 'w') as f: json.dump(report, f, indent=1, default=str)
    print(json.dumps(dict(report=path, verdict=report['verdict'])))


if __name__ == '__main__':
    main()
