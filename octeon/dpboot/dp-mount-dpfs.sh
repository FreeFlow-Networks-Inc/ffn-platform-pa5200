#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
#
# Mount the vendor master tree on the DP. Runs ON THE CP. Idempotent.
#
#   MP  /opt/dpfs                  the master, exported rw
#   CP  /opt/ffn-compat/tmp/dpfs   mounted ro  (= /tmp/dpfs inside the chroot)
#        re-exported ro, fsid=9
#   DP  /opt/dpfs                  what this script mounts
#
# WHY THIS RUNS FROM THE CP RATHER THAN ON THE DP
#
# The DP has no init that reads fstab -- pid 1 is ffn_init, which starts the
# mailbox agent and performs the root switch and nothing else. So there is no
# "on the DP" place for a persistent mount to live short of rebuilding the
# initramfs, and that is the one script whose failure strands the DP.
#
# The CP already orchestrates the DP's whole boot, so the post-switch steps
# belong here too. Call it from dp-nfsboot-debian.sh after the switch, or by
# hand afterwards; running it twice is a no-op.
#
# READ-ONLY, DELIBERATELY. The CP mounts the master ro, so rw would fail at the
# server anyway -- and vendor firmware is used in place and never modified, so
# the chain enforces the policy instead of relying on it being remembered.
set -u

DPSH=${DPSH:-/usr/local/bin/ffn-dpsh}
C=${FFN_COMPAT:-/opt/ffn-compat}
CPADDR=${CPADDR:-127.1.2.1}
EXPORT=${EXPORT:-/tmp/dpfs}          # chroot-relative: rpc.mountd runs inside $C
DPPATH=${DPPATH:-/opt/dpfs}          # where it lands on the DP
TIMEOUT=${TIMEOUT:-120}

say() { echo "  dpfs: $*"; }

[ -x "$DPSH" ] || { say "no $DPSH"; exit 2; }

# --- 1. is the CP actually serving it? ------------------------------------
if ! grep -q " $C$EXPORT nfs " /proc/mounts; then
	say "the master is not mounted at $C$EXPORT -- nothing to serve"
	exit 2
fi
if ! chroot "$C" /usr/sbin/exportfs -v 2>/dev/null | grep -q "^$EXPORT"; then
	say "$EXPORT is not exported; publishing it now"
	chroot "$C" /usr/sbin/exportfs \
		-o ro,sync,no_root_squash,no_subtree_check,fsid=9 \
		"127.1.0.0/16:$EXPORT" || { say "exportfs failed"; exit 3; }
fi

# --- 2. the mountpoint, authored on the CP's view of the DP's root ---------
# The DP's root is $C/opt/dproot here, so creating the directory here is
# creating it on the DP -- the same property the whole layering exists for.
[ -d "$C/opt/dproot$DPPATH" ] || {
	mkdir -p "$C/opt/dproot$DPPATH" && say "created the mountpoint $DPPATH"
}

# --- 3. mount it on the DP -------------------------------------------------
# Through /proc/1/root: ffn-dpsh stays on the INITRAMFS after the root switch,
# by design, so a bare $DPPATH would resolve in the initramfs -- which has no
# such directory and is not where anything will look for it. Mount namespaces
# are shared with pid 1, so mounting onto pid 1's root is both possible and
# what is wanted.
#
# `ls -l`, never `wc -c`, for the size check below: streaming bcm.user (196 MB)
# back through two nested NFS hops and the PCIe mailbox blew a 300 s marker
# timeout the first time this was done. The mount was fine; the check was not.
CMD="if grep -q ' $DPPATH nfs ' /proc/mounts; then echo ALREADY; else \
mount -t nfs -o ro,nolock,vers=3 $CPADDR:$EXPORT /proc/1/root$DPPATH \
&& echo MOUNTED || echo FAILED; fi; \
ls -l /proc/1/root$DPPATH/usr/local/cp/bcm.user 2>/dev/null | awk '{print \$5}'"

OUT=$("$DPSH" -c "$CMD" -t "$TIMEOUT" 2>&1 | grep -v '@@')

case "$OUT" in
*ALREADY*) say "already mounted on the DP" ;;
*MOUNTED*) say "mounted $CPADDR:$EXPORT on the DP at $DPPATH" ;;
*)         say "mount did not confirm:"; printf '%s\n' "$OUT" | sed 's/^/    /'; exit 1 ;;
esac

# 196484446 is bcm.user as the MP holds it. Matching that size from the DP is
# the cheap proof that all three hops carry data, not just that a mount exists.
SZ=$(printf '%s\n' "$OUT" | grep -oE '^[0-9]{6,}$' | tail -1)
if [ -n "${SZ:-}" ]; then
	say "bcm.user reads $SZ bytes from the DP"
else
	say "WARNING: could not read bcm.user size through the mount"
fi
exit 0
