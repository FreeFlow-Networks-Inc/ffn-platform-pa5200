#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""Stop the CP chrooting into the vendor CentOS tree; stay in FFN's userland.

The CP's initramfs IS FFN's own userland -- Buildroot glibc 2.41, OpenSSL
3.4.3, bash, busybox, OpenSSH -- and it runs fine on the 6.18 kernel. Then
ffn-nfsroot.sh chroots into the NFS export, which is the vendor CentOS 7.2 tree
with glibc 2.16 (2012), and everything falls over: ffn-nfsroot.sh itself died in
libc-2.16.so with SIGSEGV in an endless respawn loop, taking the shell daemon
with it, so the CP came up with no way in.

So the export is still MOUNTED -- the vendor artifacts have to stay readable in
place, bcm.user and libpandp_cp.so and the PDT Python are all needed for the
BCM and FE100 work -- but it is no longer entered. Read the vendor tree, do not
live in it.

Idempotent: re-running detects the marker and does nothing.
"""
import sys

PATH = "/mnt/clones/fwdport/rootfs/tree/sbin/ffn-nfsroot.sh"
MARKER = "FFN_STAY_IN_OUR_USERLAND"

ENTER = '''# FFN_STAY_IN_OUR_USERLAND
# The vendor export is mounted, not entered. It is CentOS 7.2 / glibc 2.16
# (2012) and its binaries SEGFAULT under this kernel -- this very script died
# in libc-2.16.so in a respawn loop -- while this initramfs is Buildroot glibc
# 2.41 + OpenSSL 3.4.3 and works. Chrooting would trade a working userland for
# a broken one and take the shell daemon down with it.
say "staying in the FFN userland; vendor tree readable at $MNT"
[ -x /sbin/ffn-cpshd ] && /sbin/ffn-cpshd &
exec $BB sh
'''


def main():
    src = open(PATH).read()
    if MARKER in src:
        print("already patched")
        return 0

    n = src.count('exec $BB chroot "$MNT" /bin/bash')
    if n == 0:
        sys.stderr.write("no chroot lines found -- has this been changed?\n")
        return 1

    # the re-entry branch: mounted already, previously chroot'ed straight in
    src = src.replace(
        '\tsay "rootfs already mounted; re-entering"\n'
        '\texec $BB chroot "$MNT" /bin/bash\n',
        '\tsay "rootfs already mounted; staying in the FFN userland"\n'
        '\t[ -x /sbin/ffn-cpshd ] && /sbin/ffn-cpshd &\n'
        '\texec $BB sh\n', 1)

    # the main path at the end of the script
    src = src.replace(
        'say "entering the NFS-backed rootfs (Python/bash/etc now run from NFS)"\n'
        'exec $BB chroot "$MNT" /bin/bash\n', ENTER, 1)

    if 'exec $BB chroot "$MNT" /bin/bash' in src:
        sys.stderr.write("a chroot line survived the rewrite; refusing to write\n")
        return 1

    open(PATH, "w").write(src)
    print("patched %s (%d chroot call(s) removed)" % (PATH, n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
