#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""Teach ffn_cpsh.py to reach the CP over SSH, keeping the telnet path.

Why: /sbin/ffn-cpshd on the CP was changed to `exec /usr/sbin/sshd -D -e`, so
the CP is now reached over SSH. The MP-side client was still a telnet client
speaking to busybox telnetd on 127.1.1.2:2323, which no longer exists there --
`ffn-cpsh` fails with "Connection refused" against any rootfs carrying the new
daemon.

Why both transports rather than a straight swap: sshd lives in the 6.18
kernel's built-in initramfs, and the 4.9 vendor NFS root has no sshd at all
(no /opt/dpfs/usr/sbin/sshd, no authorized_keys). Both kernels are in use right
now, so a client that only speaks SSH would break the 4.9 path that works
today. SSH is tried first and telnet is the fallback.

Idempotent: re-running detects the marker and does nothing.
"""
import sys

PATH = "/opt/ffn-ngfw-v2/tools/ffn_cpsh.py"
MARKER = "FFN_CPSH_SSH"

SSH_BLOCK = '''
# --- SSH transport -----------------------------------------------------
# FFN_CPSH_SSH: /sbin/ffn-cpshd now execs sshd, so this is the primary path.
#
# The identity is the MP root key (root@ffn-mp), which is what the CP's
# initramfs authorises.
#
# StrictHostKeyChecking=no with UserKnownHostsFile=/dev/null is deliberate and
# is NOT laziness: the CP's rootfs is a ramdisk, so it regenerates its host key
# on every boot. A persistent known_hosts entry would raise a host-key-changed
# warning after each reboot and refuse to connect. The link is a private PCIe
# transport to 127.1.1.2 that no physical topology can reach -- the same
# isolation the NFS export already relies on -- so pinning the key buys nothing
# and costs a broken tool every reboot.
SSH_ID = "/root/.ssh/id_ed25519"
SSH_PORT = 22
SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=8",
]


def ssh_argv(host, extra=None):
    argv = ["ssh"]
    if os.path.exists(SSH_ID):
        argv += ["-i", SSH_ID]
    argv += SSH_OPTS + ["root@%s" % host]
    if extra:
        argv.append(extra)
    return argv


def ssh_reachable(host, port=SSH_PORT):
    try:
        s = socket.create_connection((host, port), timeout=3)
        s.close()
        return True
    except OSError:
        return False


def ssh_run_one(host, command, timeout):
    """One command over SSH. Exit status is ssh's, which is the remote status."""
    try:
        p = subprocess.run(ssh_argv(host, command), timeout=timeout,
                           capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        sys.stderr.write("ffn-cpsh: timed out after %.0fs\\n" % timeout)
        return 124
    if p.stdout:
        sys.stdout.write(p.stdout)
    if p.stderr:
        sys.stderr.write(p.stderr)
    return p.returncode


def ssh_interactive(host):
    os.execvp("ssh", ssh_argv(host))


'''

NEW_DISPATCH = '''    args = ap.parse_args()

    # FFN_CPSH_SSH: prefer SSH. ffn-cpshd execs sshd now; telnetd on 2323 only
    # still exists on the older 4.9 vendor rootfs, so that stays as a fallback
    # rather than being ripped out while both kernels are in play.
    if not args.telnet and ssh_reachable(args.host):
        if args.command:
            return ssh_run_one(args.host, args.command, args.timeout)
        if not sys.stdin.isatty():
            sys.stderr.write('ffn-cpsh: stdin is not a tty; use -c "command"\\n')
            return 2
        sys.stdout.write("ffn-cpsh: CP shell over PCIe via ssh (%s)\\n" % args.host)
        sys.stdout.flush()
        ssh_interactive(args.host)          # never returns

    try:
'''


def main():
    src = open(PATH).read()
    if MARKER in src:
        print("already patched")
        return 0

    for mod in ("import subprocess\n",):
        if mod not in src:
            src = src.replace("import socket\n", "import socket\nimport subprocess\n", 1)

    anchor = "def main():"
    if anchor not in src:
        sys.stderr.write("anchor 'def main():' not found\n")
        return 1
    src = src.replace(anchor, SSH_BLOCK + anchor, 1)

    old = """    args = ap.parse_args()

    try:
"""
    if old not in src:
        sys.stderr.write("dispatch anchor not found\n")
        return 1
    src = src.replace(old, NEW_DISPATCH, 1)

    opt_anchor = '''    ap.add_argument("-t", "--timeout", type=float, default=60.0,
                    help="seconds to wait for -c output (default 60)")
'''
    if opt_anchor not in src:
        sys.stderr.write("option anchor not found\n")
        return 1
    src = src.replace(opt_anchor, opt_anchor +
                      '''    ap.add_argument("--telnet", action="store_true",
                    help="force the legacy telnetd transport (4.9 rootfs)")
''', 1)

    open(PATH, "w").write(src)
    print("patched %s" % PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
