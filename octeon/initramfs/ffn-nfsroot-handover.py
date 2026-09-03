#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""Hand the CP's shell daemon over to the chrooted root after a successful mount.

sshd is started early, from the initramfs, so that a failed NFS mount cannot
cost network access -- that is deliberate and stays. But it means an ssh
session keeps the initramfs as its root while the console session, which is
chrooted, sees the persistent NFS root. Two different filesystems depending on
how you connected: install something over ssh, reboot, find it gone; or edit a
config in one and wonder why the other disagrees. Verified on the appliance --
a marker file written into /opt/ffn-cproot was invisible to an ssh session.

So on the success path the daemon is handed over: stop the initramfs one and
start it again inside the chroot. The gap is a second, and the console is up
throughout, so nothing is stranded. On any failure path the early daemon is
simply left where it is, which is the whole point of starting it early.

Idempotent: re-running detects the marker and does nothing.
"""
import sys

PATH = "/mnt/clones/fwdport/rootfs/tree/sbin/ffn-nfsroot.sh"
MARKER = "FFN_CPSHD_HANDOVER"

BLOCK = [
    "\t# FFN_CPSHD_HANDOVER: move the shell daemon into the tree we are about to",
    "\t# enter. It was started from the initramfs so a failed mount could not cost",
    "\t# network access, but leaving it there means ssh lands in the initramfs",
    "\t# while the console lands in the persistent root -- two different",
    "\t# filesystems depending on how you connected. The console stays up across",
    "\t# the handover, so the second without a listener strands nothing.",
    "\tif [ -x \"$MNT/sbin/ffn-cpshd\" ]; then",
    "\t\t$BB pkill -f \"sshd -D\" >/dev/null 2>&1",
    "\t\t$BB sleep 1",
    "\t\t( $BB chroot \"$MNT\" /sbin/ffn-cpshd >/dev/null 2>&1 & )",
    "\t\tsay \"shell daemon handed over to the persistent root\"",
    "\tfi",
]

ANCHOR = "\t[ -x \"$MNT/sbin/ffn-fips.sh\" ] && $BB chroot \"$MNT\" /sbin/ffn-fips.sh"


def main():
    src = open(PATH).read()
    if MARKER in src:
        print("already patched")
        return 0
    lines = src.split("\n")
    if ANCHOR not in lines:
        sys.stderr.write("anchor not found -- has ffn-nfsroot.sh changed?\n")
        return 1
    i = lines.index(ANCHOR)
    lines[i:i] = BLOCK
    open(PATH, "w").write("\n".join(lines))
    print("patched %s (shell daemon handover)" % PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
