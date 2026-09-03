#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""Start the FIPS provider from the CP initramfs init, and correct a stale comment.

Two changes to /init in the CP rootfs tree.

1. Run /sbin/ffn-fips.sh once before the session loop. The FIPS provider needs
   /etc/ssl/fipsmodule.cnf, whose integrity MAC is produced by executing the
   module itself, so a cross-build host physically cannot precompute it -- the
   target is the only place it can be generated. Doing it before the loop means
   every session, and anything an operator starts, sees one crypto posture
   rather than whichever one happened to be set up first.

2. The comment above the loop still describes ffn-nfsroot.sh as ending in
   `exec chroot bash` and "re-chroots" on re-entry. That stopped being true
   when the vendor CentOS chroot was removed: the script now stays in FFN's own
   Buildroot userland and the export is merely mounted. A comment that
   confidently describes behaviour the code no longer has is worse than no
   comment, because the next person will trust it.

Idempotent: re-running detects the marker and does nothing.
"""
import sys

PATH = "/mnt/clones/fwdport/rootfs/tree/init"
MARKER = "FFN_INIT_FIPS"

FIPS = [
    "# FFN_INIT_FIPS: bring up the OpenSSL FIPS provider before any session, so",
    "# everything started afterwards sees the same crypto posture. This is also",
    "# the only place it CAN happen: fipsmodule.cnf carries an integrity MAC that",
    "# is produced by executing the module, which an x86 build host cannot do for",
    "# a mips64 binary. The script reports and returns 0 on every failure path --",
    "# it must never be the reason this board does not boot.",
    "if [ -x /sbin/ffn-fips.sh ]; then",
    "\t/bin/busybox sh /sbin/ffn-fips.sh",
    "fi",
    "",
]

OLD_COMMENT = [
    "\t# Prefer the NFS-backed userland: ffn-nfsroot.sh waits for the MP, mounts",
    "\t# /opt/dpfs from its SSD over pcnet, binds proc/sys/dev and chroots in, so",
    "\t# what runs here persists across reboots. It ends in `exec chroot bash`, so",
    "\t# it MUST stay inside this loop -- if that bash exits and init returns, the",
    "\t# kernel panics with \"Attempted to kill init!\". Re-entering is what the",
    "\t# script expects: it detects an already-mounted tree and re-chroots. On any",
    "\t# failure it falls back to a plain console itself, so a transport that is not",
    "\t# up yet can never cost us the shell.",
]

NEW_COMMENT = [
    "\t# ffn-nfsroot.sh waits for the MP, mounts /opt/dpfs from its SSD over pcnet",
    "\t# and binds proc/sys/dev. It does NOT chroot into that tree any more: the",
    "\t# export is the vendor CentOS 7.2 root whose glibc 2.16 binaries segfault",
    "\t# under this kernel, so it is mounted to be read, not entered, and the",
    "\t# session stays in this initramfs's own Buildroot userland.",
    "\t# It ends in `exec sh`, so it MUST stay inside this loop -- if that shell",
    "\t# exits and init returns, the kernel panics with \"Attempted to kill init!\".",
    "\t# Re-entering is what the script expects: it detects an already-mounted",
    "\t# tree and carries on. On any failure it falls back to a plain console",
    "\t# itself, so a transport that is not up yet can never cost us the shell.",
]

ANCHOR = 'say "verification complete; starting a shell on the console"'


def main():
    lines = open(PATH).read().split("\n")
    if any(MARKER in l for l in lines):
        print("already patched")
        return 0

    # 1. splice the FIPS bring-up in after the "verification complete" banner
    idx = None
    for i, l in enumerate(lines):
        if l.strip() == ANCHOR:
            idx = i
            break
    if idx is None:
        sys.stderr.write("anchor not found: %s\n" % ANCHOR)
        return 1
    # step past the bare `echo` that follows the banner, if present
    ins = idx + 1
    if ins < len(lines) and lines[ins].strip() == "echo":
        ins += 1
    lines[ins:ins] = FIPS

    # 2. replace the stale chroot comment, matched as a whole block so a
    #    partially-edited file is left alone rather than half-rewritten
    try:
        start = lines.index(OLD_COMMENT[0])
    except ValueError:
        sys.stderr.write("stale comment block not found; FIPS hook added, comment left alone\n")
        open(PATH, "w").write("\n".join(lines))
        return 0
    if lines[start:start + len(OLD_COMMENT)] != OLD_COMMENT:
        sys.stderr.write("comment block differs from expected; leaving it alone\n")
        open(PATH, "w").write("\n".join(lines))
        return 0
    lines[start:start + len(OLD_COMMENT)] = NEW_COMMENT

    open(PATH, "w").write("\n".join(lines))
    print("patched %s (FIPS hook + comment corrected)" % PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
