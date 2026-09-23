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

# ---- audit: WHO is resetting the control plane -------------------------------
# Restarting this unit RESETS the CP.  When that lands mid-work the CP's cores
# are stopped via stop_this_cpu() and the console prints an NMI-watchdog banner
# -- indistinguishable from a genuine hardware fault unless the cause is on
# record.  On 2026-09-03 that ambiguity cost two bcm.user runs and a long
# misdiagnosis, so every invocation now records its caller.
{
  printf 'ffn-octeon-up: START pid=%s at %s\n' "$$" "$(date -Is 2>/dev/null || date)"
  _p=$PPID
  for _ in 1 2 3 4 5; do
    [ -r "/proc/$_p/cmdline" ] || break
    printf 'ffn-octeon-up:   caller %-7s %s\n' "$_p" \
      "$(tr '\0' ' ' < "/proc/$_p/cmdline" 2>/dev/null)"
    _n=$(awk '{print $4}' "/proc/$_p/stat" 2>/dev/null)
    if [ -z "$_n" ] || [ "$_n" = 0 ] || [ "$_n" = "$_p" ]; then break; fi
    _p=$_n
    [ "$_p" = 1 ] && { printf 'ffn-octeon-up:   caller 1       (systemd)\n'; break; }
  done
} 2>/dev/null | tee -a /var/log/ffn-octeon-resets.log

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
# Current Debian initramfs banners signal when the host transport is needed.
booted_since() {
	local mark="$1"
	tail -n "+$mark" "$CL" 2>/dev/null | tr -d '\r' \
		| grep -qE 'FFN-INIT:|ffn-nfsroot: waiting'
}

# Validate inputs before stopping transport or resetting any processor.
CP_K_CONF=/etc/ffn-ngfw/octeon-kernel
cp_root_present(){
    local r
    for r in "${FFN_CP_ROOT:-/opt/ffn-nfs/cproot}" /opt/ffn-cproot; do
        [ -x "$r/bin/sh" ] && [ ! -e "$r/opt/ffn-compat" ] &&
            grep -Eq '^ID="?debian"?$' "$r/usr/lib/os-release" && return 0
    done
    return 1
}
# Select a reviewed pin explicitly; never choose a kernel by mtime or fallback.
CP_KERNEL=""
# A one-shot selection leaves the persistent pin unchanged.
if [ -n "${FFN_CP_KERNEL:-}" ]; then
	CP_KERNEL=$FFN_CP_KERNEL
	echo "CP kernel from FFN_CP_KERNEL (one-shot; $CP_K_CONF left untouched): $(basename "$CP_KERNEL")"
elif [ -r "$CP_K_CONF" ]; then
	CP_KERNEL=$(sed -n '1{s/[[:space:]]//g;p}' "$CP_K_CONF")
	if [ -n "$CP_KERNEL" ] && [ ! -s "$CP_KERNEL" ]; then
		echo "CP kernel pinned in $CP_K_CONF does not exist: $CP_KERNEL"
		echo "  No fallback kernel is allowed."
		exit 1
	fi
	[ -n "$CP_KERNEL" ] && echo "CP kernel pinned by $CP_K_CONF: $(basename "$CP_KERNEL")"
fi
if ! cp_root_present; then
    echo 'No Debian CP root staged. Set FFN_CP_ROOT to the provisioned export.'
    echo 'Build and qualify the image described in octeon/images/README.md.'
    exit 1
fi
if [ -z "$CP_KERNEL" ]; then
	echo "No CP kernel: $CP_K_CONF holds no pin and there is no fallback."
	echo "  Pin a 6.18 kernel, or pass FFN_CP_KERNEL=<path> for a one-shot."
	exit 1
fi
[ -s "$CP_KERNEL" ] || { echo "CP kernel $CP_KERNEL is missing; aborting"; exit 1; }
# Check the embedded banner, not the filename; renamed legacy images still fail.
python3 - "$CP_KERNEL" <<'PY_KERNEL' || exit 1
from pathlib import Path
import re, sys
p = Path(sys.argv[1])
if p.stat().st_size > 512 * 1024**2:
    raise SystemExit('Oversized CP kernel')
data = p.read_bytes()
if data[:6] != b'\x7fELF\x02\x02' or data[18:20] != b'\x00\x08':
    raise SystemExit('CP kernel must be MIPS64 big-endian ELF')
versions = {tuple(map(int, m)) for m in re.findall(rb'Linux version (\d+)\.(\d+)\.(\d+)', data)}
if len(versions) != 1 or next(iter(versions)) < (6, 18, 0):
    raise SystemExit('Legacy or unidentified CP kernel rejected')
PY_KERNEL
# Honour a checksum sidecar if one was staged beside the image.
if [ -s "$CP_KERNEL.md5" ]; then
	md5sum "$CP_KERNEL" | grep -q "$(cut -d' ' -f1 < "$CP_KERNEL.md5")" 		|| { echo "CP kernel checksum mismatch against $CP_KERNEL.md5; aborting"; exit 1; }
	echo "CP kernel checksum verified"
fi
# Retain the transport windows and BCM BDE DMA pool. The current kernel carries
# its own Debian initramfs; no vendor overlay or legacy memory knob is allowed.
CP_EXTRA="ffn_reserve=0x28000000,1M ffn_reserve=0x29000000,4M ffn_reserve=0x30000000,64M"
CP_OVERLAY_ARG="--no-overlay"

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
