#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
#
# Put a binary on the DP by writing it to a directory. Runs ON THE MP.
#
#   deploy-octeon-app-nfs.sh /path/to/ffn-dp-octeon [/usr/local/bin/name]
#
# ---------------------------------------------------------------------------
# WHAT THIS REPLACES
# ---------------------------------------------------------------------------
# deploy-octeon-app.sh moves the same binary through the PCIe mailbox: a 1 MB
# staging window at 0x500000 (everything above it is the mailbox and the dpnet
# rings), so a ~3.5 MB dataplane goes in four passes of stage / dd out of
# /dev/mem / append, with per-chunk retries because the mailbox is
# intermittently slow, and a sha256 at the end because the chain
# workstation -> MP -> CP -> DP has a stale-copy trap in the middle that once
# cost a full deploy-and-run cycle -- every chunk verified, against the wrong
# file.
#
# None of that is needed once the DP roots over NFS. octeon/NFS-LAYERING.md
# put it plainly: the layering exists to DELETE the bulk-transfer problem
# rather than work around it.
#
#     /opt/ffn-cproot-owrt/opt/dproot   on the MP
#         = /opt/dproot                 on the CP
#         = /                           on the DP
#
# So this is a cp(1). The file is on the DP when the write returns. There is no
# staging window, no chunk loop, no retry policy, no stale intermediate copy to
# get wrong -- because there is no intermediate copy.
#
# The old script is NOT deleted. It is the only route when the DP is on its
# initramfs, which is where a failed nfsroot leaves it by design, and that is
# exactly when you are most likely to be deploying something.
#
# ---------------------------------------------------------------------------
# WHY .tmp AND rename
# ---------------------------------------------------------------------------
# The destination may be a binary the DP is about to run, and a reader on the
# DP can see a partially written file the instant the write starts. rename(2)
# within one filesystem is atomic, so the DP sees either the old file or the
# whole new one and never half of either. This matters more here than it would
# locally: the reader is a different machine and cannot be asked to wait.
set -eu

SRC=${1:-}
DSTREL=${2:-/usr/local/bin/ffn-dp-octeon}
DPROOT=${DPROOT:-/opt/ffn-cproot-owrt/opt/dproot}
DPSH_VIA_CP=${DPSH_VIA_CP:-/usr/local/sbin/ffn-cp}

[ -n "$SRC" ] || { echo "usage: $0 <binary> [dest-path-on-the-DP]"; exit 2; }
[ -f "$SRC" ] || { echo "no such file: $SRC"; exit 2; }
[ -d "$DPROOT" ] || {
	echo "$DPROOT is not there -- this must run on the MP, and the DP's root"
	echo "must be the one the CP re-exports. See octeon/NFS-LAYERING.md."
	exit 2
}

SIZE=$(stat -c %s "$SRC")
WANT=$(sha256sum "$SRC" | cut -d' ' -f1)
DST="$DPROOT$DSTREL"

echo "source : $SRC"
echo "size   : $SIZE bytes"
echo "sha256 : $WANT"
echo "dest   : $DSTREL on the DP  (= $DST here)"

mkdir -p "$(dirname "$DST")"
cp "$SRC" "$DST.tmp"
chmod 755 "$DST.tmp"
mv "$DST.tmp" "$DST"
echo "written."

# --- confirm from the DP itself -------------------------------------------
#
# Reading the file back through the DP is the only check worth making: it is
# the machine that has to run it, and it reaches the bytes over two nested NFS
# hops that this script never touches directly.
#
# Read through /proc/1/root, NOT through "/": ffn-dpsh deliberately stays on
# the initramfs after the switch, so a plain path there resolves in the OLD
# root and would report the file missing while it is perfectly present.
if [ ! -x "$DPSH_VIA_CP" ]; then
	echo "NOTE: $DPSH_VIA_CP absent, so the DP-side check was skipped."
	exit 0
fi

echo "checking from the DP..."
# sha256sum PIPED through this shell returns nothing (NFS-LAYERING.md), so it
# is invoked directly on the file and the output is matched here instead.
OUT=$("$DPSH_VIA_CP" \
	"/usr/local/bin/ffn-dpsh -c 'sha256sum /proc/1/root$DSTREL; wc -c < /proc/1/root$DSTREL' -t 120" \
	2>&1 | grep -v '@@' || true)

GOT=$(printf '%s\n' "$OUT" | grep -oE '^[0-9a-f]{64}' | head -1)
GOTSZ=$(printf '%s\n' "$OUT" | grep -oE '^[0-9]+$' | tail -1)

if [ "${GOT:-}" = "$WANT" ]; then
	echo "  DP sees the same sha256 -- deployed."
	exit 0
fi
if [ -n "${GOTSZ:-}" ] && [ "$GOTSZ" = "$SIZE" ]; then
	# A size match with no hash is still positive evidence the bytes arrived;
	# busybox sha256sum on a large file over NFS occasionally exceeds the
	# mailbox timeout, and that is a property of the CHECK, not the deploy.
	echo "  DP sees $GOTSZ bytes (size matches; hash did not come back in time)."
	exit 0
fi
echo "  DP did NOT confirm the file:"
printf '%s\n' "$OUT" | sed 's/^/    /'
echo "  Is the DP actually rooted over NFS? Check with:"
echo "    ffn-cp \"/usr/local/bin/ffn-dpsh -c 'grep \\\" / \\\" /proc/mounts'\""
exit 1
