#!/bin/sh
# ffn-dproot -- mount the DP's real userland from an image staged in DRAM over PCIe.
#
# The DP boots on a lean initramfs (busybox + the session agent). The CP stages a
# squashfs into DP DRAM with oct-remote-load; this reads it out, mounts it, and puts a
# writable tmpfs over it.
#
# WHY SQUASHFS: the DP has ~441 MB of RAM. Unpacking a full userland into ramfs would
# consume most of it; squashfs stays COMPRESSED in memory and decompresses on demand,
# so the same tree costs a fraction of the RAM. The kernel has SQUASHFS + SQUASHFS_XZ,
# LOOP and OVERLAY_FS, which is everything this needs.
#
# WHY THE COPY: the image sits at a physical address, but the loop driver needs a
# regular file or block device -- /dev/mem is a char device and cannot be looped.
#
# SIZE IS READ FROM THE IMAGE, not hardcoded. squashfs's superblock carries bytes_used
# as a 64-bit little-endian field at offset 40 (the format is LE regardless of host
# endianness, and this host is big-endian, so it must be assembled by hand). An
# earlier version hardcoded 56 MB, which silently breaks the moment the image changes
# size.
set -u
ADDR_MB=640                 # 0x28000000, where the CP stages the image
MAX_MB=512                  # refuse anything implausible
IMG=/tmp/dproot.sqfs
LOWER=/mnt/lower
UPPER=/mnt/upper
ROOT=/mnt/root

say() { echo "ffn-dproot: $*"; }

if [ -x "$ROOT/bin/bash" ]; then
	say "already mounted; entering"
	exec chroot "$ROOT" /bin/bash -l
fi

[ -c /dev/mem ] || mknod /dev/mem c 1 1
mkdir -p "$LOWER" "$UPPER" "$ROOT"

BLK=$((ADDR_MB * 256))      # 1 MB = 256 blocks of 4096

# magic first: squashfs 4.0 begins with "hsqs"
MAGIC=$(dd if=/dev/mem bs=4096 skip=$BLK count=1 2>/dev/null | dd bs=4 count=1 2>/dev/null)
if [ "$MAGIC" != "hsqs" ]; then
	say "no squashfs at ${ADDR_MB}MB (magic '$MAGIC') -- has the CP staged it?"
	exit 1
fi

# bytes_used: 4 bytes at offset 40, little-endian (low 32 bits are enough here)
set -- $(dd if=/dev/mem bs=4096 skip=$BLK count=1 2>/dev/null |
         dd bs=1 skip=40 count=4 2>/dev/null | od -An -tu1)
BYTES=$(( $1 + ($2 * 256) + ($3 * 65536) + ($4 * 16777216) ))
SIZE_MB=$(( (BYTES + 1048575) / 1048576 ))
say "image reports $BYTES bytes; reading ${SIZE_MB}MB out of DRAM at ${ADDR_MB}MB"
if [ "$SIZE_MB" -le 0 ] || [ "$SIZE_MB" -gt "$MAX_MB" ]; then
	say "implausible size ${SIZE_MB}MB -- refusing"
	exit 1
fi

if [ ! -f "$IMG" ]; then
	dd if=/dev/mem of="$IMG" bs=1M skip=$ADDR_MB count=$SIZE_MB 2>/dev/null || {
		say "dd from /dev/mem failed"; exit 1; }
fi

modprobe loop 2>/dev/null
if ! mount -t squashfs -o ro,loop "$IMG" "$LOWER" 2>/dev/null; then
	losetup /dev/loop0 "$IMG" 2>/dev/null || losetup -f "$IMG" 2>/dev/null
	mount -t squashfs -o ro /dev/loop0 "$LOWER" || { say "squashfs mount failed"; exit 1; }
fi
say "squashfs mounted read-only at $LOWER"

mount -t tmpfs -o size=128M tmpfs "$UPPER" 2>/dev/null
mkdir -p "$UPPER/up" "$UPPER/work"
if mount -t overlay overlay -o \
	lowerdir="$LOWER",upperdir="$UPPER/up",workdir="$UPPER/work" "$ROOT" 2>/dev/null
then
	say "overlay mounted: writable userland at $ROOT"
else
	say "overlay unavailable; using the read-only squashfs directly"
	ROOT="$LOWER"
fi

for d in proc sys dev; do mkdir -p "$ROOT/$d"; done
mount -t proc  proc "$ROOT/proc" 2>/dev/null
mount -t sysfs sys  "$ROOT/sys"  2>/dev/null
mount -o bind  /dev "$ROOT/dev"  2>/dev/null

if [ "${FFN_DPROOT_NOCHROOT:-0}" = 1 ]; then
	say "mounted; not entering (FFN_DPROOT_NOCHROOT=1)"
	df -h "$ROOT" 2>/dev/null | tail -2
	echo "--- top level ---"; ls "$ROOT"
	exit 0
fi
say "entering the DP userland (exit returns to the initramfs shell)"
exec chroot "$ROOT" /bin/bash -l
