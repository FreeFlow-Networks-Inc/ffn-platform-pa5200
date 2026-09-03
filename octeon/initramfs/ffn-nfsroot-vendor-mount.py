#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""Keep the vendor tree reachable after the CP chroots into FFN's own root.

Moving the CP off the vendor CentOS root fixed the segfaults but quietly cost
something: the vendor export is mounted in the initramfs namespace, and
chrooting into /opt/ffn-cproot leaves that namespace behind. Inside the chroot
the vendor tree is simply gone -- and it is not optional. bcm.user,
libpandp_cp.so (the FE100's 5951-register CSR map is recovered from its DWARF),
the parser tables and the PDT Python all live there and are read constantly.

So it is mounted again INSIDE the tree being entered, at /mnt/vendor, and
mounted **read-only**. That is not caution for its own sake: FFN's rule is that
vendor artifacts are read in place and never modified or packaged, and an `ro`
mount enforces that rule in the kernel instead of relying on everyone
remembering it.

Failure is non-fatal and reported. Losing the vendor tree costs FE100 and BCM
work, but it must not cost the boot.

Idempotent: re-running detects the marker and does nothing.
"""
import sys

PATH = "/mnt/clones/fwdport/rootfs/tree/sbin/ffn-nfsroot.sh"
MARKER = "FFN_VENDOR_MOUNT"

BLOCK = [
    "\t# FFN_VENDOR_MOUNT: the vendor tree is still needed inside the chroot --",
    "\t# bcm.user, libpandp_cp.so, the parser tables, the PDT Python. The mount",
    "\t# made outside lives in a namespace we are about to leave, so redo it here.",
    "\t# Read-only on purpose: vendor artifacts are read in place, never modified",
    "\t# and never packaged, and ro enforces that in the kernel rather than",
    "\t# relying on everyone remembering the rule.",
    "\t$BB mkdir -p \"$MNT/mnt/vendor\"",
    "\tif /sbin/ffn_nfsmount $NFS_SERVER:/opt/dpfs \"$MNT/mnt/vendor\" \\",
    "\t     nolock,vers=3,addr=$NFS_SERVER,proto=tcp,mountproto=tcp,hard,ro >/dev/null 2>&1; then",
    "\t\tsay \"vendor tree read-only at /mnt/vendor\"",
    "\telse",
    "\t\tsay \"WARNING vendor tree not mounted -- FE100/BCM artifacts unavailable\"",
    "\tfi",
]

ANCHOR = "\tsay \"entering the FFN userland at $MNT -- changes here survive a reboot\""


def main():
    src = open(PATH).read()
    if MARKER in src:
        print("already patched")
        return 0
    lines = src.split("\n")
    if ANCHOR not in lines:
        sys.stderr.write("anchor not found -- ffn-nfsroot.sh changed?\n")
        return 1
    i = lines.index(ANCHOR)
    lines[i:i] = BLOCK
    open(PATH, "w").write("\n".join(lines))
    print("patched %s (vendor tree mounted ro inside the chroot)" % PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
