#!/bin/sh
# Mount the DP's real root over NFS from the CP, and enter it. Runs ON THE CP.
#
# Completes the layering described in octeon/NFS-LAYERING.md:
#
#     MP NFS  ->  CP OS + filesystem  ->  CP NFS  ->  DP OS + filesystem
#
# After this the DP has a full OpenWrt 24.10.4 mips64_octeonplus userspace --
# the same distribution, release and ABI as the CP -- instead of a static
# busybox initramfs.
#
# PREREQUISITES, in order. Each is checked below rather than assumed, because
# each fails as a different and misleading symptom:
#   1. ffn-dpnet-up6.sh    -- the CP<->DP link. Without it the mount just hangs.
#   2. ffn-cp-nfsd.sh      -- the CP's NFS server and the /opt/dproot export.
#   3. /opt/dproot populated on the MP at /opt/ffn-cproot-owrt/opt/dproot.
#
# WHY chroot AND NOT switch_root. The DP's kernel booted on the initramfs and
# the FFN dataplane, agent and mailbox are all running from it. switch_root
# would discard that -- including ffn-dpsh, which is how we are talking to the
# DP in the first place. chroot gives the full userspace for work that wants it
# while leaving the control path intact. A real switch_root belongs in the DP's
# boot sequence (nfsroot= on the kernel command line), not in a live session.
#
# THE ERROR THIS SCRIPT EXISTS TO EXPLAIN. Running a binary from the mount
# WITHOUT chroot fails as:
#
#     sh: /mnt/dproot/bin/busybox: not found
#
# The file is plainly there. What is missing is its ELF interpreter: OpenWrt's
# binaries are dynamically linked against /lib/ld-musl-mips64-sf.so.1, and from
# the initramfs root that path resolves into the initramfs, which has no musl.
# "not found" naming a file that exists almost always means the interpreter,
# not the binary.
set -eu

DPSH=${DPSH:-/usr/local/bin/ffn-dpsh}
CPADDR=${CPADDR:-127.1.2.1}
EXPORT=${EXPORT:-/opt/dproot}
MNT=${MNT:-/mnt/dproot}
CMD=${1:-}

say() { echo "  dp-nfsroot: $*"; }
dp() { "$DPSH" -c "$1" -t "${2:-90}" 2>&1; }

# --- 1. the link -----------------------------------------------------------
if ping -c1 -W2 127.1.2.2 >/dev/null 2>&1; then
	say "CP<->DP link up"
else
	say "DP does not answer on 127.1.2.2 -- run ffn-dpnet-up6.sh first"
	exit 2
fi

# --- 2. the export ---------------------------------------------------------
if /usr/sbin/exportfs -v 2>/dev/null | grep -qF "$EXPORT"; then
	say "$EXPORT is exported"
else
	say "$EXPORT is not exported -- run ffn-cp-nfsd.sh first"
	exit 2
fi
[ -d "$EXPORT/bin" ] || { say "$EXPORT looks empty -- populate it on the MP"; exit 2; }

# --- 3. mount on the DP ----------------------------------------------------
# `nolock` deliberately: the CP's own root uses it, and lockd across two nested
# NFS re-exports is not something to inherit by accident.
if dp "mount | grep -c ' $MNT '" 20 | grep -q '^1'; then
	say "already mounted on the DP"
else
	dp "mkdir -p $MNT; mount -t nfs -o nolock,vers=3 $CPADDR:$EXPORT $MNT" 120 >/dev/null
	if dp "mount | grep -c ' $MNT '" 20 | grep -q '^1'; then
		say "mounted $CPADDR:$EXPORT on the DP at $MNT"
	else
		say "mount FAILED -- check 'cat /proc/filesystems | grep nfs' on the DP"
		exit 3
	fi
fi

# --- 3b. make the chroot habitable ----------------------------------------
# A squashfs root ships no runtime state, and OpenWrt normally creates it in
# preinit -- which never runs here. Without /var/lock, opkg fails with
#
#     opkg_conf_load: Could not create lock file /var/lock/opkg.lock
#
# which reads like a permissions or read-only-root problem rather than a missing
# directory. These are created in the EXPORT (so on the MP's disk, persistently)
# rather than inside the chroot, because the mount is the same filesystem and
# doing it once is enough.
for d in var/lock var/run var/log var/state tmp proc sys dev; do
	[ -d "$EXPORT/$d" ] || mkdir -p "$EXPORT/$d"
done

# /proc, /sys and /dev must come from the DP's own kernel, not the export --
# they are per-machine and the export is shared. Bind-mounting the DP's live
# ones in is what makes the chroot a working environment rather than a file
# viewer: without /proc, anything that reads /proc/self or /proc/mounts fails in
# confusing ways.
dp "for d in proc sys dev; do
        mountpoint -q $MNT/\$d || mount --bind /\$d $MNT/\$d 2>/dev/null
    done; mount | grep -c \"$MNT/\"" 90 >/dev/null

# --- 4. prove it is usable -------------------------------------------------
# Executing through chroot is the only meaningful check: the mount succeeding
# says nothing about whether the interpreter resolves.
OUT=$(dp "chroot $MNT /bin/busybox uname -sm" 60 | tr -d '\r')
case "$OUT" in
	*mips64*) say "chroot works: $(printf '%s' "$OUT" | grep -o 'Linux.*mips64')" ;;
	*) say "chroot did NOT run a binary; got: $OUT"; exit 4 ;;
esac
# Read the release from the EXPORT rather than back through the chroot. The
# mailbox interleaves its own framing with command output, so scraping a value
# out of it is unreliable -- an earlier version parsed this from the DP and
# printed an empty string on a run where everything had in fact worked. The
# export is the same bytes and is local.
say "release: $(sed -n "s/^DISTRIB_RELEASE='\(.*\)'/\1/p" "$EXPORT/etc/openwrt_release" 2>/dev/null)"

# --- 5. optional command ---------------------------------------------------
if [ -n "$CMD" ]; then
	echo
	dp "chroot $MNT /bin/sh -c '$CMD'" 180
else
	cat <<EOF

Run a command inside the DP's real root:
  $0 'opkg list-installed | wc -l'
  $0 'ls /usr/bin'

Interactively, from the CP:
  $DPSH        then:  chroot $MNT /bin/sh

NOTE the DP has no route to the MP's package mirror yet -- opkg inside the
chroot will not reach 127.1.1.1:8080 until the DP has a default route via
$CPADDR and the CP forwards. Adding packages meanwhile is done on the MP,
into /opt/ffn-cproot-owrt$EXPORT, where it appears here immediately.
EOF
fi
