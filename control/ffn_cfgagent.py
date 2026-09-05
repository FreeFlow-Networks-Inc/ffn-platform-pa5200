#!/usr/bin/env python
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""ffn_cfgagent -- CP-side config agent, and the DP's only route to the MP.

Runs on the CP. Pulls the versioned key/value set from ffn_cfgd on the MP,
applies what belongs to the CP, and forwards what belongs to the DP.

Written for **python2**: the CP is CentOS 7.2 (mips64 big-endian) and
/usr/bin/python is 2.7. It is kept 2/3-clean so it also runs under python3 for
testing on the MP, but python2 is the target and nothing here may assume
otherwise.

Why the DP leg goes through this agent
--------------------------------------
There is no IP path from anywhere to the DP. The CP has no ffn_dpnet interface,
and the DP's only Ethernet is the 40G to the BCM, which carries data, not
management. The one control channel is the PCIe mailbox, reached with
`ffn-dpsh`. So the CP is not merely a convenient relay point, it is the only
one, and DP config is store-and-forward by necessity rather than by choice.

The DP also has no interpreter -- busybox and nothing else -- so what gets
pushed there is a plain key=value file plus a shell apply hook, never a script
that assumes python.

Convergence model: poll VERSION (one line each way), and only GET when it
changes. A node that reboots re-pulls on its own instead of waiting to be
noticed, which matters because the MP cannot reach the DP to push.
"""
import argparse
import os
import socket
import subprocess
import sys
import time

DEFAULT_SERVER = "127.1.1.1"
DEFAULT_PORT = 7420
CP_CONF = "/etc/ffn/cp.env"
DP_CONF = "/etc/ffn/dp.env"          # staged here, then pushed to the DP
DP_PUSHED = "/etc/ffn/.dp.pushed"   # what the DP was last CONFIRMED to hold
CP_HOOKS = "/etc/ffn/apply.d"
DP_REMOTE = "/etc/ffn/dp.env"        # where it lands ON the DP
DPSH = "/usr/local/bin/ffn-dpsh"


def ask(server, port, request, timeout=10.0):
    """One request, one response. Returns the raw text, or None on failure."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((server, port))
        s.sendall(request.encode("ascii") if hasattr(request, "encode") else request)
        chunks = []
        while True:
            b = s.recv(4096)
            if not b:
                break
            chunks.append(b)
            if b"\n.\n" in b"".join(chunks) or request.startswith("VERSION"):
                break
        return b"".join(chunks).decode("utf-8", "replace")
    except Exception:
        return None
    finally:
        try:
            s.close()
        except Exception:
            pass


def parse(text):
    """Split a GET response into (version, [key=value, ...])."""
    ver = None
    pairs = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("VERSION "):
            try:
                ver = int(line.split()[1])
            except (IndexError, ValueError):
                pass
        elif line == ".":
            break
        elif "=" in line:
            pairs.append(line)
    return ver, pairs


def write_if_changed(path, lines):
    """Write only on real change, so apply hooks do not fire spuriously."""
    body = "".join(l + "\n" for l in lines)
    try:
        with open(path, "r") as fh:
            if fh.read() == body:
                return False
    except IOError:
        pass
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(body)
    os.rename(tmp, path)          # atomic, so a reader never sees a half file
    return True


def run_hooks(directory, env_path):
    if not os.path.isdir(directory):
        return
    for name in sorted(os.listdir(directory)):
        p = os.path.join(directory, name)
        if os.access(p, os.X_OK):
            try:
                subprocess.call([p, env_path])
            except Exception as exc:
                sys.stderr.write("hook %s failed: %s\n" % (p, exc))


def push_to_dp(lines, verbose=False):
    """Store-and-forward the DP's config over the PCIe mailbox.

    ffn-dpsh mangles nested quoting, so the file is written a line at a time
    with simple appends rather than one big heredoc. Slower, but it survives
    the quoting rules; correctness beats elegance on a channel this awkward.
    """
    if not os.path.exists(DPSH):
        return False
    cmds = ["mkdir -p %s" % os.path.dirname(DP_REMOTE),
            "rm -f %s.tmp" % DP_REMOTE]
    for l in lines:
        if "'" in l:                      # refuse rather than mis-quote
            sys.stderr.write("skipping key with quote: %s\n" % l)
            continue
        cmds.append("echo '%s' >> %s.tmp" % (l, DP_REMOTE))
    cmds.append("mv %s.tmp %s" % (DP_REMOTE, DP_REMOTE))
    try:
        rc = subprocess.call([DPSH, "-c", "; ".join(cmds)],
                             stdout=None if verbose else open(os.devnull, "w"),
                             stderr=subprocess.STDOUT)
        return rc == 0
    except Exception as exc:
        sys.stderr.write("dp push failed: %s\n" % exc)
        return False


def push_to_dp_if_needed(lines, verbose=False):
    """Push to the DP unless the DP already has exactly this content.

    Convergence must be driven by what was last SUCCESSFULLY delivered, not by
    whether the CP's staged copy changed this cycle. Gating on the staged file
    means a failed push is never retried: the staging write succeeds, the push
    fails, and on the next cycle the staged file matches so nothing happens and
    the DP stays stale forever. That bug was real and is why this marker exists.
    """
    body = "".join(l + "\n" for l in lines)
    try:
        with open(DP_PUSHED, "r") as fh:
            if fh.read() == body:
                return True
    except IOError:
        pass
    if verbose:
        print("dp out of date (%d keys) -> pushing over the mailbox" % len(lines))
    if not push_to_dp(lines, verbose):
        sys.stderr.write("dp push failed; will retry next cycle\n")
        return False
    d = os.path.dirname(DP_PUSHED)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(DP_PUSHED, "w") as fh:
        fh.write(body)
    if verbose:
        print("dp push confirmed")
    return True


def cycle(server, port, verbose=False):
    text = ask(server, port, "GET cp\n")
    if text is None:
        return None
    ver, pairs = parse(text)
    if ver is None:
        return None

    cp_lines = [p for p in pairs if p.startswith("cp.") or p.startswith("all.")]
    dp_lines = [p for p in pairs if p.startswith("dp.") or p.startswith("all.")]

    cp_changed = write_if_changed(CP_CONF, cp_lines)
    dp_changed = write_if_changed(DP_CONF, dp_lines)

    # Hooks run when EITHER file changes, not cp.env alone.
    #
    # Some DP-scoped config is applied BY THE CP on the dataplane's behalf,
    # because only the CP can reach the hardware: the forwarding fabric is
    # dp.fabric.* but the BCM88375 is on the CP's PCIe bus, so 50-fabric runs
    # here and reads dp.env.
    #
    # Firing on cp.env alone meant a pure fabric change staged into dp.env and
    # then sat there -- the key arrived, no hook ran, the port kept its old
    # state, and nothing anywhere reported a problem. That is the worst shape a
    # config system can fail in, so the condition is widened rather than having
    # 50-fabric poll.
    if cp_changed or dp_changed:
        if verbose:
            print("config changed (cp=%d dp=%d keys, cp_changed=%s dp_changed=%s)"
                  " -> running hooks"
                  % (len(cp_lines), len(dp_lines), cp_changed, dp_changed))
        run_hooks(CP_HOOKS, CP_CONF)

    push_to_dp_if_needed(dp_lines, verbose)

    return ver


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    if a.once:
        ver = cycle(a.server, a.port, a.verbose)
        if ver is None:
            sys.stderr.write("ffn_cfgagent: no answer from %s:%d\n"
                             % (a.server, a.port))
            return 1
        print("converged on version %d" % ver)
        return 0

    seen = -1
    while True:
        text = ask(a.server, a.port, "VERSION\n", timeout=5.0)
        if text is not None:
            try:
                ver = int(text.split()[1])
            except (IndexError, ValueError):
                ver = None
            if ver is not None and ver != seen:
                got = cycle(a.server, a.port, a.verbose)
                if got is not None:
                    seen = got
                    print("converged on version %d" % got)
                    sys.stdout.flush()
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main() or 0)
