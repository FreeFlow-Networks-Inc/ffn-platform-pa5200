#!/bin/bash
# ffn-octeon-up -- host (MP) orchestration to bring the OCTEON CP up NFS-rooted.
#
# The OCTEON runs a full userland from the MP's SSD over NFS-over-pcnet; this is
# the host half. The OCTEON's own init auto-runs /sbin/ffn-nfsroot, which waits
# for the MP; this script provides what it waits for.
#
# HARDENED against the oct-remote-* fragility (they wedge the serial in
# uninterruptible-D if two run at once or one is killed, which shows up as
# `sha256 MISMATCH` staging). The strategy is to be the SOLE oct-remote user
# while booting:
#   * an exclusive lock (ffn-octlock.sh) means only one orchestration runs and no
#     standalone pcnet-up races it -- this alone prevents the double-boot that
#     produced the mismatches;
#   * every oct-remote step runs SEQUENTIALLY (reset, then stage, then window);
#   * readiness is read from the CONSOLE LOG, not cpdp -- cpdp itself shells out
#     to oct-remote-csr (WindowedDram reprograms its BAR window per op), so using
#     it to poll would spawn oct-remote calls that race the staging;
#   * oct-remote is NEVER killed (a D-state op is unkillable; killing leaves the
#     wedge). If a prior op is genuinely stuck, this aborts rather than piling on.
set -u
cd /opt/ffn-ngfw-v2
. tools/ffn-octlock.sh
CL=/var/log/ffn-octeon-console.log
LOG=/var/log/ffn-octeon-up.log
exec >>"$LOG" 2>&1
echo "=== ffn-octeon-up $(date) ==="

# Only one orchestration at a time; a second exits rather than double-booting.
if ! octlock_acquire 8; then
	echo "another ffn-octeon-up (or pcnet-up) holds the lock; exiting"
	exit 0
fi
trap 'octlock_release' EXIT

# Readiness = the OCTEON's own init banner / NFS-root line appearing in the
# console log AFTER we start watching. No oct-remote involved.
console_mark() { wc -l < "$CL" 2>/dev/null || echo 0; }
# The readiness patterns are per-kernel and BOTH must be here. 'it booted'
# and 'NFS-root: /sbin/ffn-nfsroot' are what the 4.9 CP prints. The 6.18 CP
# prints 'FFN-INIT:' and then 'ffn-nfsroot: waiting up to 300s for the MP'
# instead, so a 4.9-only pattern never fires: the wait times out after 180s,
# the script exits 1, pcnet-up never runs, and the CP sits waiting for a host
# end that is never brought up. 'ffn-nfsroot: waiting' is in fact the ideal
# trigger -- it is printed at exactly the moment the host end is needed.
booted_since() {
	local mark="$1"
	tail -n "+$mark" "$CL" 2>/dev/null | tr -d '\r' \
		| grep -qE 'it booted|NFS-root: /sbin/ffn-nfsroot|FFN-INIT:|ffn-nfsroot: waiting'
}

# 1. console broker (single owner of /dev/ttyS1).
#    NEVER call `ffn_octconsoled.py start` from here: it does NOT daemonize --
#    serve() becomes the broker in the foreground. Doing that blocked this
#    script on every cold boot (no broker was running yet), and systemd's
#    TimeoutStartSec then TERMed the whole cgroup, killing the broker with it.
#    The broker is ffn-octconsoled.service, which this unit Requires; the code
#    below only covers a hand-run of this script outside systemd.
broker_up() {
	python3 tools/ffn_octconsoled.py status 2>/dev/null | grep -q 'running pid'
}
if ! broker_up; then
	if ! systemctl start ffn-octconsoled.service 2>/dev/null; then
		setsid python3 tools/ffn_octconsoled.py start >/dev/null 2>&1 &
	fi
	for i in 1 2 3 4 5; do
		broker_up && break
		sleep 1
	done
fi
if ! broker_up; then
	echo "console broker is not running and could not be started; aborting"
	exit 1
fi
sleep 1

# 2. boot the OCTEON. We do not probe cpdp first (that would spawn oct-remote);
#    instead, always (re)stage -- ffn_octctl/ffn_octboot are safe to re-run, and
#    on an already-up OCTEON this simply reboots it cleanly into the same flow.
MARK=$(console_mark)
echo "resetting + staging FFN kernel over PCIe (sole oct-remote user)"
# Stop the host end of pcnet BEFORE the reset. Resetting the OCTEON while
# ffn_pcnetd is polling its BAR window produced a PCIe Completion-Timeout ->
# AER storm that took the MP down entirely on 2026-09-02 -- "AER: can't recover
# (no error_detected callback)" -- and needed a physical power cycle. Nothing is
# lost by stopping it: pcnet-up.sh below brings it back and reprograms BAR1
# index 1, which the reset clears regardless.
systemctl stop ffn-pcnetd 2>/dev/null || true
for _i in 1 2 3 4 5; do
	systemctl is-active --quiet ffn-pcnetd || break
	sleep 1
done
if systemctl is-active --quiet ffn-pcnetd; then
	echo "ABORT: ffn-pcnetd is still active; refusing to reset the OCTEON under a live BAR writer"
	exit 1
fi
echo "host ffn-pcnetd stopped (PCIe CmpltTO/AER hazard)"
python3 tools/ffn_octctl.py boot --dev 0 --force
# mem= is REQUIRED. Without it the kernel takes whatever the OCTEON boot
# descriptor offers, which is ~432 MB of the 8 GB this CP actually has
# (device tree: 0x0+0x10000000 and 0x20000000+0x1F0000000). The suffix
# matters -- memparse() reads a bare mem=2048 as 2048 BYTES.
# ffn_reserve=0x30000000,64M is the BCM88375 BDE DMA pool (ffn_bde dma_phys=).
# The vendor SDK needs far more than the 4 MB dma_alloc_coherent can give on
# this kernel (MAX_ZONEORDER 11, no CMA): DNX init completes on 4 MB and then
# bcm_petra_rx_init fails with Out of memory. 0x30000000 is inside the
# 0x29400000-0x7fefffff System RAM range, below 4 GB (SBUSDMA host addresses
# are 32-bit), clear of the rootfs (0x22000000) and transport (0x28/0x29000000).
# --- which CP kernel, and the args that go with it --------------------------
# 6.18 is preferred, but "bootable" means more than the image existing: a 6.18
# CP mounts its userland over NFS from the MP, so with no staged CP root it
# reaches a bare console shell with no tools. The 4.9 image carries its own
# vendor root and always comes up. So require BOTH a 6.18 image AND a usable
# CP root before choosing it, and let a one-line file override everything.
CP_K_CONF=/etc/ffn-ngfw/octeon-kernel
CP_K_49=/var/lib/ffn-ngfw/octeon/ffn-vmlinux-octeon3

cp_root_present(){
	for r in /opt/ffn-nfs/cproot-owrt /opt/ffn-cproot-owrt 	         /opt/ffn-nfs/cproot /opt/ffn-cproot; do
		[ -x "$r/bin/sh" ] && return 0
	done
	return 1
}
# NO mtime heuristic here on purpose. "Newest 6.18 image" is NOT "good 6.18
# image": at the time of writing the newest staged one was
# ffn-vmlinux-6.18.49-msi, which panics on this chip with
#   Kernel panic - not syncing: request_irq(OCTEON_IRQ_PCI_MSI0) failed
# because CONFIG_PCI_MSI pulls in CIU-era msi-octeon.c and this is OCTEON III.
# Picking by date would have panicked every boot. So 6.18 must be named
# EXPLICITLY, and anything unnamed falls back to the kernel that always works.
# provision.sh writes the pin once a 6.18 image and a CP root are both staged.
CP_KERNEL=""
if [ -r "$CP_K_CONF" ]; then
	CP_KERNEL=$(sed -n '1{s/[[:space:]]//g;p}' "$CP_K_CONF")
	if [ -n "$CP_KERNEL" ] && [ ! -s "$CP_KERNEL" ]; then
		echo "CP kernel pinned in $CP_K_CONF does not exist: $CP_KERNEL"
		echo "  falling back to 4.9 rather than refusing to boot the CP"
		CP_KERNEL=""
	fi
	[ -n "$CP_KERNEL" ] && echo "CP kernel pinned by $CP_K_CONF: $(basename "$CP_KERNEL")"
fi
if [ -n "$CP_KERNEL" ] && ! cp_root_present; then
	echo "WARNING: $CP_K_CONF pins a 6.18 kernel but no CP root is staged."
	echo "  6.18 has no userland of its own -- it mounts one over NFS -- so the CP"
	echo "  will reach a console shell with no tools. Stage one with"
	echo "  octeon/cproot/ffn-cp-owrt-stage.sh, or clear the pin for 4.9."
fi
if [ -z "$CP_KERNEL" ]; then
	CP_KERNEL=$CP_K_49
	echo "CP kernel: 4.9 (no pin in $CP_K_CONF)"
fi
[ -s "$CP_KERNEL" ] || { echo "CP kernel $CP_KERNEL is missing; aborting"; exit 1; }
# Honour a checksum sidecar if one was staged beside the image.
if [ -s "$CP_KERNEL.md5" ]; then
	md5sum "$CP_KERNEL" | grep -q "$(cut -d' ' -f1 < "$CP_KERNEL.md5")" 		|| { echo "CP kernel checksum mismatch against $CP_KERNEL.md5; aborting"; exit 1; }
	echo "CP kernel checksum verified"
fi
# ffn_mem=auto is a 4.9-only knob and is obsolete upstream; 6.18 needs no mem=
# at all, since nothing clamps max_memory there and it sees all 8 GB.
# 0x30000000,64M stays in BOTH: it is the BCM88375 BDE DMA pool, and without it
# the SDK falls back to a 4 MB dma_alloc_coherent and bcm_petra_rx_init fails
# with Out of memory.
# The OVERLAY matters as much as the args. ffn_octboot stages an overlay rootfs
# at 0x22000000 by default and passes ffn_rootfs=/ffn_reserve= for it. The 6.18
# kernel carries its OWN embedded initramfs, so handing it the 4.9-era overlay
# on top faults on an unaligned load during init and panics:
#     do_ade / handle_adel_int
#     Kernel panic - not syncing: Attempted to kill the idle task!
# Every working 6.18 boot passed --no-overlay; dropping that when this moved
# into the service path is what panicked the CP.
case "$CP_KERNEL" in
	*6.18*)
		CP_EXTRA="ffn_reserve=0x28000000,1M ffn_reserve=0x29000000,4M ffn_reserve=0x30000000,64M"
		CP_OVERLAY_ARG="--no-overlay"
		;;
	*)
		CP_EXTRA="ffn_mem=auto,256M ffn_reserve=0x28000000,1M ffn_reserve=0x29000000,4M ffn_reserve=0x30000000,64M"
		CP_OVERLAY_ARG=""
		;;
esac
echo "CP boot: $(basename "$CP_KERNEL") ${CP_OVERLAY_ARG:-with overlay}"
python3 tools/ffn_octboot.py --watch 150 --fdt "" $CP_OVERLAY_ARG --kernel "$CP_KERNEL" --extra "$CP_EXTRA" &
BOOTW=$!

echo "waiting for the OCTEON init banner on the console ..."
up=0
for i in $(seq 1 90); do
	if booted_since "$MARK"; then up=1; break; fi
	sleep 2
done
if [ "$up" != 1 ]; then
	echo "OCTEON did not reach its init banner; aborting"
	exit 1
fi
echo "OCTEON kernel is up (init banner seen)"

# 3. host end of pcnet: program the window (still the sole oct-remote user -- the
#    staging above has finished by the time the banner prints), start the daemon,
#    publish the magic. pcnet-up inherits our lock (FFN_OCTEON_LOCKED).
echo "bringing up host pcnet"
bash tools/pcnet-up.sh
sleep 3
systemctl is-active --quiet ffn-pcnetd \
	&& echo "host pcnet up; the OCTEON's ffn-nfsroot will mount and chroot" \
	|| echo "WARNING host pcnet daemon not active"

# Give the OCTEON's ffn-nfsroot a moment to catch the now-up MP and mount.
for i in $(seq 1 24); do
	tail -40 "$CL" 2>/dev/null | tr -d '\r' | grep -qE 'entering the NFS' && break
	sleep 5
done
if tail -60 "$CL" 2>/dev/null | tr -d '\r' | grep -qE 'entering the NFS'; then
	echo "OCTEON entered the NFS-backed rootfs"
fi
# 4. CP shell over PCIe: telnetd bound to 127.1.1.2 ONLY. The CP holds no IP on
#    eth0/eth1, so there is no physical-topology path to this port -- same
#    isolation the NFS export relies on. Driven from the MP because the chrooted
#    bash startup files are not a reliable hook.
if tail -60 "$CL" 2>/dev/null | tr -d '
' | grep -qE 'entering the NFS'; then
	# install FFN's own copy into the vendor rootfs (BYO firmware: the repo
	# is the source of truth, /opt/dpfs is only the delivery point)
	install -m 0755 octeon/cpsh/ffn-cpshd /opt/dpfs/sbin/ffn-cpshd 2>/dev/null
	echo "starting the CP shell service (ffn-cpshd) -- reach it with: ffn-cpsh"
	printf '/sbin/ffn-cpshd
' > /run/ffn-octeon-console.in
	sleep 3
fi

wait "$BOOTW" 2>/dev/null || true
echo "=== ffn-octeon-up done $(date) ==="
