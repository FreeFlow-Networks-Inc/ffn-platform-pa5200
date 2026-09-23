#!/bin/bash
# ffn-nfsroot -- on the OCTEON: bring up the PCIe virtual ethernet, NFS-mount the
# MP's CP root, and enter it. FFN's "unified storage" model: the OCTEON runs a
# full Linux userland served from the MP's SSD over NFS-over-pcnet.
#
# Run automatically by ffn_init at boot, so it MUST be safe to auto-run:
# idempotent (re-entering after the chrooted shell exits just re-enters), and on
# ANY failure it drops to a plain console instead of exiting -- a boot must never
# lose its shell to a transport that is not up yet.
#
# The lean initramfs has busybox applets but not all on PATH, so everything is
# called as `busybox <applet>`. The host must be running ffn_pcnetd (pcnet-up.sh)
# so the region magic is published and 127.1.1.1 answers.
BB=busybox
NFS_SERVER=127.1.1.1
MNT=/tmp/cproot

# One explicit export, supplied by the MP in the image's boot environment.
# There is no fallback to a different distribution or a recovered vendor root.
NFS_CANDIDATES=${FFN_CP_EXPORT:-}

# 90s could never work: the host end cannot come up until the boot script's
# --watch window closes and releases /run/ffn-octeon-ctl.lock. Measured 165s on
# the first real 6.18 boot.
MOUNT_WAIT=300       # seconds to wait for the MP before giving up to a shell

say() { echo "ffn-nfsroot: $*"; }
# NOTE: this exec REPLACES the script, so init's respawn loop cannot retry the
# mount -- the console parks here. That is deliberate (a boot must never lose its
# shell to a transport that is not up yet), but it means the mount has to be
# finished by hand, so say how. ffn_pcnetd is supervised separately in init and
# recovers on its own regardless.
fallback() {
	say "$* -- dropping to a console"
	say "to finish the mount once the MP answers: sh /sbin/ffn-nfsroot.sh"
	exec $BB sh
}

# Validate before entering a mounted root, including a previously mounted root.
enter() {
	local sh
	$BB grep -Eq '^ID="?debian"?$' "$MNT/usr/lib/os-release" || fallback 'Debian root required'
	[ ! -e "$MNT/opt/ffn-compat" ] || fallback 'compatibility roots are retired'
	for sh in /bin/bash /bin/sh; do
		if [ -x "$MNT$sh" ]; then
			say "entering the NFS-backed rootfs via $sh"
			exec $BB chroot "$MNT" "$sh"
		fi
	done
	fallback "mounted tree has no usable shell"
}

[ -n "$NFS_CANDIDATES" ] || fallback 'MP must provision FFN_CP_EXPORT'

# Already mounted (a previous run)? Just re-enter it.
if [ -e "$MNT/bin/busybox" ] || [ -e "$MNT/bin/bash" ]; then
	say "rootfs already mounted; re-entering"
	enter
fi

# 0. start the CP<->DP transport daemon if present, so the host can use
#    memrd/memwr diagnostics while pcnet comes up.
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

# 2. wait for the MP to answer over the link
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

# 3. mount whichever CP root this appliance actually exports
$BB mkdir -p $MNT
mounted=""
for exp in $NFS_CANDIDATES; do
	say "trying $NFS_SERVER:$exp"
	if /sbin/ffn_nfsmount "$NFS_SERVER:$exp" "$MNT" \
	     nolock,vers=3,addr=$NFS_SERVER,proto=tcp,mountproto=tcp,hard >/dev/null 2>&1 \
	   && [ -e "$MNT/bin" ]; then
		mounted=$exp
		break
	fi
	# Leave nothing half-mounted for the next candidate to trip over.
	$BB umount "$MNT" 2>/dev/null || $BB umount -l "$MNT" 2>/dev/null
done
[ -n "$mounted" ] || fallback "no CP root mounted (tried:$NFS_CANDIDATES)"
say "mounted $mounted over PCIe"

# 4. bind the pseudo-filesystems so the NFS userland is fully live. Do NOT skip
#    sysfs: without it /sys/bus/pci reads stale directories off the NFS tree and
#    a PCI listing silently lies.
for d in proc sys dev; do $BB mkdir -p "$MNT/$d"; done
$BB mount -t proc  proc "$MNT/proc" 2>/dev/null
$BB mount -t sysfs sys  "$MNT/sys"  2>/dev/null
$BB mount -o bind  /dev "$MNT/dev"  2>/dev/null
enter
