#!/bin/bash
# ffn-nfsroot -- on the OCTEON: bring up the PCIe virtual ethernet, NFS-mount the
# MP's full rootfs, and enter it. FFN's "unified storage" model: the OCTEON runs
# a full Linux userland served from the MP's SSD over NFS-over-pcnet.
#
# Run automatically by ffn_init at boot. So it MUST be safe to auto-run: idempotent
# (re-entering after the chrooted shell exits just re-enters), and on ANY failure
# it drops to a plain console instead of exiting -- a boot must never lose its
# shell to a transport that is not up yet.
#
# The lean initramfs has busybox applets but not all on PATH, so everything is
# called as `busybox <applet>`. The host must be running ffn_pcnetd (pcnet-up.sh)
# so the region magic is published and 127.1.1.1 answers; the waits below give it
# generous time to come up after this OCTEON boots.
BB=busybox
NFS_SERVER=127.1.1.1
# The CP's own root on the appliance's dedicated mirrored NFS volume
# (install-to-disk.sh creates it; provision.sh exports it to the PCIC
# subnet only). NOT /opt/dpfs: that is the vendor CentOS 7 tree, whose
# glibc is 2.16 and which SIGSEGVs the instant bash starts on a 6.18
# kernel. The mount worked; the binaries did not. cproot/ holds a
# glibc-2.41 Buildroot tree -- the same userland the initramfs runs, so
# already proven on this kernel. /opt/dpfs stays exported for anything
# that needs the vendor tree.
NFS_EXPORT=/opt/ffn-nfs/cproot
MNT=/tmp/cproot
# 90s could never work: the host end cannot come up until the boot
# script's --watch window closes and releases /run/ffn-octeon-ctl.lock.
# Measured 165s on the first real 6.18 boot.
MOUNT_WAIT=300       # seconds to wait for the MP before giving up to a shell

say() { echo "ffn-nfsroot: $*"; }
# NOTE: this exec REPLACES the script, so init's respawn loop cannot retry
# the mount -- the console parks here. That is deliberate (a boot must never
# lose its shell to a transport that is not up yet), but it means the mount
# has to be finished by hand, so say how. ffn_pcnetd is supervised
# separately in init and recovers on its own regardless.
fallback() {
	say "$* -- dropping to a console"
	say "to finish the mount once the MP answers: sh /sbin/ffn-nfsroot.sh"
	exec $BB sh
}

# Already mounted (a previous run)? Just re-enter it.
if [ -e "$MNT/bin/bash" ]; then
	say "rootfs already mounted; re-entering"
	exec $BB chroot "$MNT" /bin/bash
fi

# 0. start the CP<->DP transport daemon if present. ffn_init used to start it;
#    since this flow now runs in its place, start it here so the host can detect
#    the OCTEON over cpdp and use memrd/memwr diagnostics while pcnet comes up.
if [ -x /sbin/ffn_cpdpd ] && ! $BB pidof ffn_cpdpd >/dev/null 2>&1; then
	say "starting ffn_cpdpd"
	/sbin/ffn_cpdpd >/tmp/ffn_cpdpd.log 2>&1 &
	sleep 1
fi

# 1. bring up the OCTEON end of pcnet (creates ffnnet0 @ 127.1.1.2)
if ! $BB pidof ffn_pcnetd >/dev/null 2>&1; then
	say "starting ffn_pcnetd"
	/sbin/ffn_pcnetd >/tmp/ffn_pcnetd.log 2>&1 &
fi

# 2. wait for the MP to answer over the link (host brings up its end in parallel)
say "waiting up to ${MOUNT_WAIT}s for the MP at $NFS_SERVER ..."
ok=0
for i in $($BB seq 1 $MOUNT_WAIT); do
	if $BB ping -c 1 -W 1 $NFS_SERVER >/dev/null 2>&1; then
		ok=1; break
	fi
	sleep 1
done
[ "$ok" = 1 ] || fallback "MP never answered"
say "MP reachable"

# 3. mount the full rootfs over NFS
$BB mkdir -p $MNT
say "mounting $NFS_SERVER:$NFS_EXPORT -> $MNT"
/sbin/ffn_nfsmount $NFS_SERVER:$NFS_EXPORT $MNT \
	nolock,vers=3,addr=$NFS_SERVER,proto=tcp,mountproto=tcp,hard,intr
[ -e "$MNT/bin" ] || fallback "mount produced an empty tree"
say "mounted; the MP rootfs is now visible over PCIe"

# 4. enter it. Bind the pseudo-filesystems so the NFS userland is fully live.
for d in proc sys dev; do $BB mkdir -p "$MNT/$d"; done
$BB mount -t proc  proc "$MNT/proc" 2>/dev/null
$BB mount -t sysfs sys  "$MNT/sys"  2>/dev/null
$BB mount -o bind  /dev "$MNT/dev"  2>/dev/null
say "entering the NFS-backed rootfs (Python/bash/etc now run from NFS)"
exec $BB chroot "$MNT" /bin/bash
