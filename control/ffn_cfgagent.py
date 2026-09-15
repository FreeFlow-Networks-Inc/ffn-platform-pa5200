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
import re
import socket
import subprocess
import sys
import time

DEFAULT_SERVER = "127.1.1.1"
DEFAULT_PORT = 7420
CP_CONF = "/etc/ffn/cp.env"
DP_CONF = "/etc/ffn/dp.env"          # staged here, then pushed to the DP
DP_PUSHED = "/etc/ffn/.dp.pushed"   # what the DP was last CONFIRMED to hold
DP_PUSHED_AT = "/etc/ffn/.dp.pushed.at"   # when, so a DP reboot can be detected
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


# The mailbox carries a command line, not a file, and it is NOT a bulk
# transport: it is one shared /bin/sh reached through a 64 KB window, and
# octeon/dpnet2/DPNET.md records that bulk data through it does not arrive
# intact. So the push is chunked.
#
# These numbers come from a real failure, not from taste. Sending all of a
# 30-key dp.env as one command produced ~1.5 KB of shell in a single mailbox
# round trip and timed out:
#
#     RuntimeError: no marker within 30s; partial output was: ...
#
# with the partial output showing the command truncated mid-line. 400 bytes
# per chunk is comfortably under whatever the real ceiling is, and 8 commands
# keeps a chunk readable in a log when one of them fails.
DP_PUSH_CHUNK_BYTES = 400
DP_PUSH_CHUNK_CMDS = 8
DP_PUSH_TIMEOUT = "90"


def _chunk(cmds):
    """Group shell commands into mailbox-sized batches."""
    batch, size = [], 0
    for c in cmds:
        # +2 for the "; " that will join them.
        if batch and (size + len(c) + 2 > DP_PUSH_CHUNK_BYTES
                      or len(batch) >= DP_PUSH_CHUNK_CMDS):
            yield batch
            batch, size = [], 0
        batch.append(c)
        size += len(c) + 2
    if batch:
        yield batch


def push_to_dp(lines, verbose=False):
    """Store-and-forward the DP's config over the PCIe mailbox.

    ffn-dpsh mangles nested quoting, so the file is written a line at a time
    with simple appends rather than one big heredoc. Slower, but it survives
    the quoting rules; correctness beats elegance on a channel this awkward.

    The appends are then sent in CHUNKS, because one command holding every
    line is bulk data and the mailbox does not carry bulk -- see
    DP_PUSH_CHUNK_BYTES above for the failure that established that.

    Writes to a .tmp and renames at the end, so a push interrupted halfway
    leaves the DP's previous dp.env intact rather than a half-file that its
    apply hook would read as truth.
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

    batches = list(_chunk(cmds))
    devnull = None if verbose else open(os.devnull, "w")
    try:
        for i, batch in enumerate(batches, 1):
            try:
                # ffn-dpsh is SINGLE-SESSION: one shared /bin/sh on the DP,
                # and concurrent clients wedge it. These calls are therefore
                # strictly sequential, never parallelised for speed.
                rc = subprocess.call(
                    [DPSH, "-c", "; ".join(batch), "-t", DP_PUSH_TIMEOUT],
                    stdout=devnull, stderr=subprocess.STDOUT)
            except Exception as exc:
                sys.stderr.write("dp push chunk %d/%d raised: %s\n"
                                 % (i, len(batches), exc))
                return False
            if rc != 0:
                # Name the chunk. A push that fails at chunk 9 of 12 has
                # already written eight chunks' worth into the .tmp, and
                # knowing where it stopped is the difference between a
                # diagnosis and a guess.
                sys.stderr.write("dp push failed at chunk %d/%d (rc=%d); "
                                 "%s.tmp left in place, %s untouched\n"
                                 % (i, len(batches), rc, DP_REMOTE, DP_REMOTE))
                return False
            if verbose:
                sys.stderr.write("  dp push chunk %d/%d ok\n" % (i, len(batches)))
        return True
    finally:
        if devnull is not None:
            devnull.close()


# The DP's /etc/ffn lives in its INITRAMFS, which is tmpfs, so a DP reboot
# destroys the pushed config while the CP's "I delivered it" marker survives on
# the CP's own NFS root. The DP then runs with no config at all and nothing
# anywhere reports a problem -- the same silent-drift shape the marker was
# introduced to prevent, just one layer further out.
#
# This is not hypothetical: after the DP was re-rooted over NFS, /etc/ffn did
# not exist on the DP in either root while .dp.pushed on the CP still matched.
#
# Note that pushing into the initramfs rather than into the NFS root is
# CORRECT and deliberate -- ffn-dpsh stays on the initramfs by design so the
# control channel cannot be taken down by a bad export. The config therefore
# lives where the control channel can always reach it, and the cost is that it
# is volatile. So the agent has to detect the reboot rather than assume
# persistence.
#
# Detection ASKS THE DP what it holds rather than inferring it. Comparing the
# DP's uptime against the time since the push also works and needs no file
# read, but it is inference: it answers "could the DP still hold this?" when
# the question is "does it?". It is also useless on the transition, because an
# agent that has never written a timestamp has nothing to compare against --
# which is exactly the stale state this change has to heal.
DP_VERIFY_INTERVAL = 60.0    # seconds between checks, to spare a shared mailbox
_dp_last_verify = [0.0]      # list so it can be rebound under python2


def _dpsh_capture(cmd, timeout="30"):
    """Run one command on the DP and return its output, or None."""
    if not os.path.exists(DPSH):
        return None
    try:
        p = subprocess.Popen([DPSH, "-c", cmd, "-t", timeout],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = p.communicate()
    except Exception:
        return None
    if not isinstance(out, str):
        out = out.decode("utf-8", "replace")
    return out


# ffn-dpsh ECHOES the command before the reply, so any marker that appears in
# the command text matches the echo and proves nothing -- that exact mistake
# made the unwedge tool report a wedged shell as healthy. The reply here is a
# byte count the DP's shell COMPUTES, and a line holding nothing but an integer
# cannot be the echo of a command that contains words.
_COUNT_RE = re.compile(r"^\s*(-?\d+)\s*$", re.M)

# `wc -c` and not a checksum: NFS-LAYERING.md records that `sha256sum` piped
# through this shell returns nothing at all while `ls` in the same command
# works. A byte count catches the cases that actually occur -- the file gone
# after a DP reboot, or a push truncated by the mailbox -- and it costs one
# small command on a channel that is shared and single-session.
#
# `expr 0 - 1` is the not-found arm rather than `echo MISSING`, for the same
# echo reason: -1 is computed, the word MISSING would be quoted back at us.
DP_SIZE_CMD = "wc -c < %s 2>/dev/null || expr 0 - 1"


def dp_holds(expected_body, verbose=False):
    """Does the DP still hold what we last confirmed? True / False / None.

    None means "cannot tell" and must be treated as "do not act": a wrong True
    leaves the dataplane unconfigured, and a wrong False re-pushes on every
    cycle over a mailbox other things need.
    """
    out = _dpsh_capture(DP_SIZE_CMD % DP_REMOTE)
    if not out:
        return None                       # unreachable -> cannot tell
    m = None
    for m in _COUNT_RE.finditer(out):     # the LAST bare integer is the answer
        pass
    if m is None:
        return None
    got = int(m.group(1))
    want = len(expected_body)
    if got == want:
        return True
    if verbose:
        why = "absent" if got < 0 else "%d bytes, expected %d" % (got, want)
        print("dp does not hold the pushed config (%s)" % why)
    return False


def dp_lost_config_since_push(verbose=False):
    """True when the DP demonstrably no longer holds the confirmed config."""
    now = time.time()
    if now - _dp_last_verify[0] < DP_VERIFY_INTERVAL:
        return False                      # rate-limited; the mailbox is shared
    _dp_last_verify[0] = now
    try:
        with open(DP_PUSHED, "r") as fh:
            body = fh.read()
    except IOError:
        return False
    return dp_holds(body, verbose) is False


def push_to_dp_if_needed(lines, verbose=False):
    """Push to the DP unless the DP already has exactly this content.

    Convergence must be driven by what was last SUCCESSFULLY delivered, not by
    whether the CP's staged copy changed this cycle. Gating on the staged file
    means a failed push is never retried: the staging write succeeds, the push
    fails, and on the next cycle the staged file matches so nothing happens and
    the DP stays stale forever. That bug was real and is why this marker exists.

    ...and "last delivered" is not the same as "still held", because the DP's
    copy is in tmpfs and does not survive its reboot. See
    dp_lost_config_since_push above.
    """
    body = "".join(l + "\n" for l in lines)
    try:
        with open(DP_PUSHED, "r") as fh:
            if fh.read() == body and not dp_lost_config_since_push(verbose):
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
    # Purely an operator-visible record of when the dataplane last took a
    # config. Nothing reads it: convergence asks the DP what it holds rather
    # than reasoning about elapsed time, so this file can be deleted or stale
    # without changing any decision.
    with open(DP_PUSHED_AT, "w") as fh:
        fh.write("%.0f\n" % time.time())
    _dp_last_verify[0] = time.time()
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


def reconcile_dp(verbose=False):
    """Re-assert the DP's staged config if the DP no longer holds it.

    Reads the staged copy rather than the last GET, so it works on a cycle
    where nothing was fetched at all -- which is every cycle on a box whose
    config is not being edited.
    """
    try:
        with open(DP_CONF, "r") as fh:
            lines = [l for l in fh.read().splitlines() if l.strip()]
    except IOError:
        return False                  # nothing staged yet
    if not lines:
        return False
    return push_to_dp_if_needed(lines, verbose)


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
        # Reconcile the DP even when the version has NOT moved.
        #
        # The DP loses its config by rebooting, not by the MP changing its
        # mind, and those are independent events. Driving the DP leg only from
        # version changes means a steady-state box -- the normal case -- never
        # re-checks, so a dataplane that came back empty stays empty until
        # somebody happens to edit the config. That is precisely the silent
        # drift the confirmation marker exists to prevent, so the check cannot
        # be gated on the same signal.
        #
        # This is cheap: push_to_dp_if_needed short-circuits on the CP-side
        # marker, and the DP is only asked once per DP_VERIFY_INTERVAL.
        reconcile_dp(a.verbose)
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main() or 0)
