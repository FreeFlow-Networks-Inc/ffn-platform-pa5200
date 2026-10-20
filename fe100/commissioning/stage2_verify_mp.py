#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""Stage 2 proof: the chip's return path reaches the dataplane's native decoder.

With the lab attachment (front 13, zone 99) installed by production_attach_cp.py and
nothing production steered, this makes BCM port 20 the RAW force-forwarded return to
the DP trunk (24), opens the lab port's MAC loop so frames injected from the DP enter
the FE100, runs the native punt probe on the DP with the exact return binding, injects
nonce-tagged frames that have no session (so the chip answers each with FLOWUNKNOWN
to the miss next hop), and expects the probe to decode them. Everything is restored in
reverse afterwards; production ports are never touched.

Runs on the MP: python3 stage2_verify_mp.py [--count 6] [--keep-return-path]
"""
import argparse
import json
import os
import select
import subprocess
import sys
import time
import uuid

sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)), '/tmp']
import first_session_mp as fs

LAB_FRONT, LAB_PORT, LAB_ZONE, TRUNK, RETURN = 13, 7, 99, 24, 20
STAGE = getattr(fs, 'STAGE', '/tmp')     # where the helpers live on each plane
DP_SSH = next((c for c in (STAGE + '/ffn_dp_ssh.py', STAGE + '/ffn-dp-ssh.py') if os.path.exists(c)), STAGE + '/ffn_dp_ssh.py')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--count', type=int, default=6)
    p.add_argument('--keep-return-path', action='store_true', help='leave port 20 RAW -> 24 commissioned afterwards')
    a = p.parse_args()
    from pathlib import Path
    fs.LOG = Path('/var/log/ffn-fe100-stage2-' + str(time.time_ns()) + '.json')
    fs.report = dict(schema=1, kind='stage2-return-path', steps=[], cleanup_errors=[])
    report = fs.report
    mac = None; baseline_started = False; path_enabled = False; return_enabled = False; previous_header = None
    nonce = uuid.uuid4().hex
    try:
        # the chip's return path: port 20 RAW, force-forwarded to the DP trunk
        # the lab port's MAC loop and the session path 7 -> 3 (as the first-session runs)
        mac = subprocess.Popen(['/usr/local/sbin/ffn-cp', 'cd /; python3 /usr/local/sbin/ffn_fe100_mac_lab.py --serve --port %d' % LAB_FRONT],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if not select.select([mac.stdout], [], [], 120)[0]: raise RuntimeError('MAC lab did not answer')
        first = json.loads(mac.stdout.readline())
        if first.get('ready') is not True: raise RuntimeError('MAC lab: ' + json.dumps(first)[:300])
        fs.step('mac-loop', ready=True)
        base = fs.bcm_lab('baseline-begin', ports=[LAB_PORT]); baseline_started = True
        fs.step('baseline-begin', result=base)
        path = fs.bcm_lab('session-path-enable'); path_enabled = True
        fs.step('session-path-enable', markers=path.get('markers'))
        # the lab's session path refuses an owned port 20, so the chip's return comes up after it
        state = fs.cp_op('return-path-status')
        before = state['state']['FFN_RETURN']
        previous_header = int(before['header'])
        if before['enabled'] == '1' and before['destination'] != str(TRUNK):
            raise RuntimeError('port 20 is force-forwarded elsewhere: ' + json.dumps(before))
        fs.step('return-path-before', **before)
        enabled = fs.cp_op('return-path-enable')
        if not enabled.get('done'): raise RuntimeError('return path enable failed: ' + json.dumps(enabled))
        return_enabled = True
        fs.step('return-path-enable', **enabled['state']['FFN_RETURN'])
        # the probe on the DP, then the injection while it listens
        probe_cmd = ('/tmp/punt-probe ffnpkt0 %d %d %d %d %d %d %s 2600' % (TRUNK, RETURN, LAB_FRONT, LAB_FRONT, LAB_ZONE, LAB_PORT, nonce))
        probe = subprocess.Popen(['python3', DP_SSH, 'bash', '-c', probe_cmd],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if not select.select([probe.stdout], [], [], 30)[0]: raise RuntimeError('probe did not start')
        ready = probe.stdout.readline().strip()
        fs.step('probe-ready', line=ready[:200])
        rc, out, err = fs.dp('python3 /tmp/dp_inject.py %d %s %d' % (LAB_FRONT, nonce, a.count), timeout=30)
        fs.step('inject', rc=rc, out=out.strip()[:200], err=err.strip()[-200:])
        try:
            out, err = probe.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            probe.kill(); out, err = probe.communicate()
        line = [l for l in out.splitlines() if l.startswith('{"packets"')]
        result = json.loads(line[-1]) if line else {'raw': out[-400:], 'err': err[-300:]}
        decoded = []
        for hexframe in result.get('packets', []):
            raw = bytes.fromhex(hexframe)
            decoded.append(dict(length=len(raw), ethernet=raw[:14].hex(), ip_src='.'.join(str(b) for b in raw[26:30]) if len(raw) >= 34 else None,
                                nonce=nonce in hexframe))
        fs.step('probe-result', captured=result.get('captured'), decoded=result.get('decoded'), rejected=result.get('rejected'),
                expired=result.get('expired'), rx=result.get('rx'), failed=result.get('failed'), frames=decoded[:8], raw=result.get('raw'))
        report['verdict'] = ('PASS' if result.get('captured', 0) == a.count and not result.get('failed') else
                             'FAIL: captured %s of %s' % (result.get('captured'), a.count))
    except Exception as error:
        report['error'] = str(error)
    finally:
        cleanup = report.setdefault('cleanup_errors', [])
        if path_enabled:
            try: fs.step('session-path-restore', **fs.bcm_lab('session-path-restore'))
            except Exception as e: cleanup.append('session-path-restore: ' + str(e))
        if baseline_started:
            try: fs.step('baseline-end', **fs.bcm_lab('baseline-end', ports=[LAB_PORT]))
            except Exception as e: cleanup.append('baseline-end: ' + str(e))
        if mac is not None:
            try:
                mac.stdin.write(json.dumps({'op': 'finish'}) + '\n'); mac.stdin.flush()
                if select.select([mac.stdout], [], [], 60)[0]: fs.step('mac-restore', result=json.loads(mac.stdout.readline()))
                mac.stdin.close(); mac.wait(timeout=30)
            except Exception as e: cleanup.append('mac restore: ' + str(e))
        if return_enabled and not a.keep_return_path:
            try: fs.step('return-path-restore', **fs.cp_op('return-path-restore', {'header': previous_header})['state']['FFN_RETURN'])
            except Exception as e: cleanup.append('return-path-restore: ' + str(e))
        fs.save()
        print(json.dumps(dict(report=str(fs.LOG), verdict=report.get('verdict'), error=report.get('error'), cleanup_errors=cleanup)))


if __name__ == '__main__':
    main()
