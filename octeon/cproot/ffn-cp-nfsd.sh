#!/bin/sh
# Turn the CP into an NFS server that re-exports the DP's root. Runs ON THE CP.
#
# THE LAYERING THIS COMPLETES
#
#     MP NFS  ->  CP OS + filesystem  ->  CP NFS  ->  DP OS + filesystem
#
# The CP already roots over NFS from the MP:
#     127.1.1.1:/opt/ffn-cproot-owrt  /  nfs  vers=3
# and the DP's root lives INSIDE that tree, at /opt/dproot. So the DP's
# filesystem is authored on the MP -- write to
# /opt/ffn-cproot-owrt/opt/dproot there and it appears at /opt/dproot here,
# with no transfer step at all.
#
# WHY THAT MATTERS MORE THAN IT SOUNDS. The alternative was moving files to the
# DP over the PCIe mailbox, one megabyte at a time, through a single shared
# /bin/sh. A 3.4 MB image would not survive it intact -- individual chunks
# verified and the whole-file hash did not -- and `sha256sum` piped in that
# shell returns nothing at all. This design removes that path from the bulk
# case entirely: the MP writes, the CP sees, the DP mounts.
#
# THE ONE OPTION THAT IS NOT OPTIONAL: fsid=
#
# /opt/dproot sits on an NFS mount, so this is an NFS RE-EXPORT. nfsd cannot
# derive a filesystem identifier for a filesystem it does not own, and without
# an explicit fsid= the export is refused. Linux has supported re-export since
# 5.11; this kernel is 6.18, so the support is there and only the fsid has to be
# supplied. Pick a number and keep it stable -- changing it invalidates every
# client's file handles, which presents as stale-handle errors rather than as a
# configuration change.
#
# nfsd.ko IS OURS, not OpenWrt's. CONFIG_NFSD was unset in the CP kernel while
# every dependency (EXPORTFS, GRACE_PERIOD, LOCKD, LOCKD_V4, SUNRPC) was already
# built in, so it builds as a single module against the running kernel --
# vermagic 6.18.49-dirty -- exactly the way i2c-dev did. opkg's
# nfs-kernel-server pulls kmod-fs-nfsd built for OpenWrt's OWN kernel and
# refuses to install at all (not even with --nodeps: it rejects the package as
# arch-incompatible before dependency resolution). So the userspace binaries are
# extracted from the .ipk and the module is ours.
set -eu

DPROOT=${DPROOT:-/opt/dproot}
NET=${NET:-127.1.0.0/16}
FSID=${FSID:-7}
THREADS=${THREADS:-8}
MOD=${MOD:-/lib/modules/nfsd.ko}

say() { echo "  nfsd: $*"; }

# --- 1. the module ---------------------------------------------------------
if grep -qw nfsd /proc/filesystems; then
	say "nfsd already registered"
else
	[ -f "$MOD" ] || { say "no $MOD -- build it: scripts/config --module NFSD"; exit 2; }
	insmod "$MOD" || { say "insmod failed"; exit 3; }
	grep -qw nfsd /proc/filesystems || { say "loaded but not registered"; exit 3; }
	say "loaded $MOD"
fi

# --- 2. state directories --------------------------------------------------
# exportfs fails with "could not open /var/lib/nfs/.etab.lock: errno 2" if these
# are absent, which reads like a permissions problem rather than a missing path.
mkdir -p /var/lib/nfs/v4recovery /var/lib/nfs/sm /var/lib/nfs/sm.bak
for f in etab rmtab state; do [ -e "/var/lib/nfs/$f" ] || : > "/var/lib/nfs/$f"; done

# --- 3. the export ---------------------------------------------------------
[ -d "$DPROOT" ] || { say "$DPROOT does not exist -- populate it on the MP at
       /opt/ffn-cproot-owrt$DPROOT"; exit 2; }

LINE="$DPROOT ${NET}(rw,sync,no_root_squash,no_subtree_check,fsid=$FSID)"
if [ -f /etc/exports ] && grep -qF "$DPROOT " /etc/exports; then
	say "export already in /etc/exports"
else
	printf '%s\n' "$LINE" >> /etc/exports
	say "added: $LINE"
fi

# --- 4. daemons ------------------------------------------------------------
# Order matters: rpcbind first (nfsd and mountd register with it), then nfsd,
# then exportfs to publish the table, then mountd to answer MNT requests.
pidof rpcbind >/dev/null 2>&1 || { /usr/sbin/rpcbind; sleep 1; }
[ "$(cat /proc/fs/nfsd/threads 2>/dev/null || echo 0)" -gt 0 ] \
	|| { /usr/sbin/rpc.nfsd "$THREADS"; sleep 1; }
/usr/sbin/exportfs -ra
pidof rpc.mountd >/dev/null 2>&1 || { /usr/sbin/rpc.mountd; sleep 1; }

# --- 5. report what is actually true --------------------------------------
say "threads : $(cat /proc/fs/nfsd/threads 2>/dev/null)"
say "versions: $(cat /proc/fs/nfsd/versions 2>/dev/null)"
say "listening: $(netstat -lnt 2>/dev/null | grep -c ':2049 ') socket(s) on 2049"
echo
/usr/sbin/exportfs -v

cat <<EOF

Verify from the MP, which reaches this host over ffnnet0 -- that exercises the
whole chain rather than just this end:

  mount -t nfs -o nolock,vers=3,ro 127.1.1.2:$DPROOT /mnt/dptest
  file /mnt/dptest/bin/busybox        # expect ELF 64-bit MSB MIPS64
  umount /mnt/dptest
EOF
