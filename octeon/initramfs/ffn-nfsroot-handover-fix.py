#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""Make the CP shell-daemon handover actually work, and report it honestly.

The first version used `pkill -f "sshd -D"`. This busybox has **no pkill
applet**, so the kill silently did nothing: the initramfs sshd kept running,
the chrooted ffn-cpshd found `pidof sshd` already true and exited, and ssh
carried on serving the initramfs while the console served the persistent root.
The console nevertheless printed "shell daemon handed over to the persistent
root", because the message was unconditional -- it claimed an outcome it had
never checked. That is the worse half of the bug: a silent failure that
announces success sends the next person looking in the wrong place.

pidof IS present (ffn-cpshd depends on it), so the kill goes through that, the
old daemon is actually waited for, and the message now reflects what happened.

Idempotent: re-running detects the marker and does nothing.
"""
import sys

PATH = "/mnt/clones/fwdport/rootfs/tree/sbin/ffn-nfsroot.sh"
MARKER = "FFN_HANDOVER_PIDOF"

OLD = [
    "\t\t$BB pkill -f \"sshd -D\" >/dev/null 2>&1",
    "\t\t$BB sleep 1",
    "\t\t( $BB chroot \"$MNT\" /sbin/ffn-cpshd >/dev/null 2>&1 & )",
    "\t\tsay \"shell daemon handed over to the persistent root\"",
]

NEW = [
    "\t\t# FFN_HANDOVER_PIDOF: this busybox has no pkill applet -- using it made",
    "\t\t# the handover a no-op that still reported success. pidof is present.",
    "\t\tpid=$($BB pidof sshd 2>/dev/null)",
    "\t\t[ -n \"$pid\" ] && $BB kill $pid >/dev/null 2>&1",
    "\t\ti=0",
    "\t\twhile [ $i -lt 10 ] && $BB pidof sshd >/dev/null 2>&1; do",
    "\t\t\t$BB sleep 1",
    "\t\t\ti=$((i + 1))",
    "\t\tdone",
    "\t\tif $BB pidof sshd >/dev/null 2>&1; then",
    "\t\t\t# ffn-cpshd would see it and exit, so say so rather than pretend.",
    "\t\t\tsay \"WARNING initramfs sshd would not stop; ssh stays in the initramfs\"",
    "\t\t\tsay \"  the persistent root is still reachable: chroot /tmp/dpfs\"",
    "\t\telse",
    "\t\t\t( $BB chroot \"$MNT\" /sbin/ffn-cpshd >/dev/null 2>&1 & )",
    "\t\t\t$BB sleep 2",
    "\t\t\tsay \"shell daemon handed over to the persistent root\"",
    "\t\tfi",
]


def main():
    src = open(PATH).read()
    if MARKER in src:
        print("already patched")
        return 0
    lines = src.split("\n")
    try:
        i = lines.index(OLD[0])
    except ValueError:
        sys.stderr.write("pkill line not found -- already changed?\n")
        return 1
    if lines[i:i + len(OLD)] != OLD:
        sys.stderr.write("handover block differs from expected; leaving it alone\n")
        return 1
    lines[i:i + len(OLD)] = NEW
    open(PATH, "w").write("\n".join(lines))
    print("patched %s (pidof-based handover, honest reporting)" % PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
