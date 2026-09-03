#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""Fix three faults in the CP's ffn-nfsroot.sh, all seen on one 6.18 boot.

1. NFS_EXPORT contradicted its own comment. The comment documents
   /opt/ffn-cproot -- which exists on the MP, is exported, and holds FFN's own
   Buildroot tree -- while the value said /opt/ffn-nfs/cproot, a path that was
   never created. The mount failed with errno 13 and the board fell back to a
   bare console.

2. The shell daemon started only AFTER a successful mount, so a mount failure
   cost all network access. That is backwards: ffn-cpshd lives in this
   initramfs and needs nothing from NFS. It now starts as soon as the MP
   answers, before the mount is attempted, so the fallback path keeps ssh.

3. Chrooting was disabled wholesale when the export was the vendor CentOS 7.2
   tree, whose glibc 2.16 binaries SIGSEGV on this kernel. With the export
   corrected to FFN's own glibc 2.41 tree that ban is too broad -- it also
   threw away the persistence that was the point of mounting a root off the
   MP's SSD. So the chroot returns, guarded: enter the tree only if it looks
   like an FFN Buildroot userland, otherwise stay in the initramfs. The lesson
   is kept as a check rather than a prohibition.

Idempotent: re-running detects the marker and does nothing.
"""
import sys

PATH = "/mnt/clones/fwdport/rootfs/tree/sbin/ffn-nfsroot.sh"
MARKER = "FFN_NFSROOT_FIXED"

CPSHD = [
    "",
    "# FFN_NFSROOT_FIXED: start the shell daemon BEFORE attempting the mount.",
    "# It lives in this initramfs and needs nothing from NFS, so gating network",
    "# access on the mount succeeding was backwards -- when the mount failed with",
    "# EACCES this board dropped to a console with no way in but the serial line.",
    "# Backgrounded in a subshell because ffn-cpshd execs `sshd -D`, which would",
    "# otherwise take over this script's console.",
    "if [ -x /sbin/ffn-cpshd ]; then",
    "\t( /sbin/ffn-cpshd >/tmp/ffn-cpshd.log 2>&1 & )",
    "\tsay \"shell daemon starting -- reach this CP with: ssh root@127.1.1.2\"",
    "else",
    "\tsay \"WARNING /sbin/ffn-cpshd is not in this initramfs; console only\"",
    "fi",
]

ENTER = [
    "# Enter the tree only if it is FFN's own userland. /opt/dpfs, the vendor",
    "# CentOS 7.2 export, is still mounted elsewhere for the binaries and tables",
    "# we read out of it, but its glibc 2.16 (2012) SIGSEGVs on this kernel --",
    "# chrooting into it put init in a respawn loop and cost this board its shell.",
    "# Checking os-release is cheap, and makes the bad case \"stay in the",
    "# initramfs\" rather than \"no working userland at all\".",
    "if $BB grep -qs \"^ID=buildroot\" \"$MNT/etc/os-release\"; then",
    "\tsay \"entering the FFN userland at $MNT -- changes here survive a reboot\"",
    "\texec $BB chroot \"$MNT\" /bin/sh -l",
    "fi",
    "say \"$MNT is not an FFN userland -- mounted, not entered\"",
    "say \"staying in the initramfs; it is proven on this kernel\"",
    "exec $BB sh",
]


def main():
    src = open(PATH).read()
    if MARKER in src:
        print("already patched")
        return 0

    lines = src.split("\n")

    # 1. the export path
    n = 0
    for i, l in enumerate(lines):
        if l.startswith("NFS_EXPORT="):
            lines[i] = "NFS_EXPORT=/opt/ffn-cproot"
            n += 1
    if n != 1:
        sys.stderr.write("expected exactly one NFS_EXPORT line, found %d\n" % n)
        return 1

    # 2. shell daemon right after the MP answers
    try:
        idx = lines.index('say "MP reachable"')
    except ValueError:
        sys.stderr.write("anchor 'say \"MP reachable\"' not found\n")
        return 1
    lines[idx + 1:idx + 1] = CPSHD

    # 3. replace the stay-put block with the guarded chroot
    start = None
    for i, l in enumerate(lines):
        if l.startswith("# FFN_STAY_IN_OUR_USERLAND"):
            start = i
            break
    if start is None:
        sys.stderr.write("FFN_STAY_IN_OUR_USERLAND block not found\n")
        return 1
    end = start
    while end < len(lines) and lines[end].strip() != "exec $BB sh":
        end += 1
    if end >= len(lines):
        sys.stderr.write("could not find the end of the stay-put block\n")
        return 1
    lines[start:end + 1] = ENTER

    open(PATH, "w").write("\n".join(lines))
    print("patched %s (export, early cpshd, guarded chroot)" % PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
