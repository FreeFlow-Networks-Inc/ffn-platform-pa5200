#!/bin/sh
# Ensure the kernel filesystems are mounted inside the build chroot.
# Idempotent, and LOUD on failure. Run it before every bootstrap invocation.
#
# WHY THIS IS ITS OWN SCRIPT. The wrapper used to do it inline as three lines,
# each ending in `2>/dev/null || true`, and that combination cost a build:
#
#   * /dev itself was never in the list -- only /dev/pts -- and
#   * a failed mount said nothing at all.
#
# It surfaced 133 source packages in, when util-linux's build-deps pulled the
# host systemd and its postinst could not configure:
#
#     Setting up systemd (262~rc1-2) ...
#     Cannot open '/etc/machine-id' in neither writable nor read-only mode:
#         Function not implemented
#     dpkg: error processing package systemd (--configure):
#      old systemd package postinst maintainer script subprocess failed
#
# ENOSYS names nothing useful and sent me looking at the filesystem (ext4, fine),
# at the file's mode (0444 -- making it writable changed nothing), and at the
# kernel version (7.0.0, far newer than systemd 262 needs) before I checked
# whether anything was mounted at all. It was not: `mount | grep -c "$CHROOT"`
# returned 0. Bind-mounting proc, sys, dev and dev/pts fixed it immediately --
# systemd-machine-id-setup then succeeded silently and dpkg --configure systemd
# moved the package from iF to ii.
#
# CHECK THE MOUNTS BEFORE BELIEVING ANY CHROOT DIAGNOSTIC. This is the second
# time on this project that an empty /proc or /sys produced a confident,
# misleading error.
#
# The mounts do not survive a host reboot, and relaunching the bootstrap by hand
# bypasses whatever the wrapper did earlier -- which is exactly how this chroot
# ended up bare. Hence a standalone script that is safe and cheap to re-run.
set -u

CHROOT="${1:-/mnt/clones/debian-mips64/sid-host}"

if [ ! -d "$CHROOT" ]; then
	echo "ensure-chroot-mounts: no such chroot: $CHROOT" >&2
	exit 2
fi

# Refuse to touch mounts while a build is running: unmounting or re-mounting
# under a live dpkg is a good way to corrupt a package database.
if ps -eo args 2>/dev/null | grep -q "[b]ootstrap.sh HOST_ARCH"; then
	echo "ensure-chroot-mounts: a bootstrap is running -- refusing to change" \
	     "mounts underneath it" >&2
	exit 3
fi

fail=0

# /dev with --rbind so its submounts (pts, shm) come along; the others are plain
# binds. Order matters only in that /dev must precede anything under it.
for spec in "/proc proc rbind" "/sys sys rbind" "/dev dev rbind"; do
	src=$(echo "$spec" | cut -d' ' -f1)
	dst=$(echo "$spec" | cut -d' ' -f2)
	mode=$(echo "$spec" | cut -d' ' -f3)
	target="$CHROOT/$dst"

	mkdir -p "$target" 2>/dev/null || sudo mkdir -p "$target"

	if mountpoint -q "$target"; then
		echo "  already mounted: /$dst"
		continue
	fi
	if sudo mount --"$mode" "$src" "$target"; then
		echo "  mounted: /$dst"
	else
		echo "  FAILED to mount /$dst" >&2
		fail=1
	fi
done

# Verify rather than assume. A mount that reports success but leaves the
# directory empty is the case that produced the original misleading error.
for d in proc sys dev; do
	if ! mountpoint -q "$CHROOT/$d"; then
		echo "  NOT MOUNTED after attempting: /$d" >&2
		fail=1
	elif [ -z "$(ls -A "$CHROOT/$d" 2>/dev/null)" ]; then
		echo "  MOUNTED BUT EMPTY: /$d" >&2
		fail=1
	fi
done

# The specific thing that broke, checked directly. Cheap, and it fails here
# rather than 133 packages into a build.
if [ "$fail" = 0 ]; then
	if sudo chroot "$CHROOT" systemd-machine-id-setup >/dev/null 2>&1; then
		echo "  verified: systemd-machine-id-setup works in the chroot"
	else
		echo "  WARNING: systemd-machine-id-setup still fails -- a systemd" \
		     "postinst will not configure" >&2
		fail=1
	fi
fi

[ "$fail" = 0 ] && echo "ensure-chroot-mounts: OK"
exit "$fail"
