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
booted_since() {
	local mark="$1"
	tail -n "+$mark" "$CL" 2>/dev/null | tr -d '\r' \
		| grep -qE 'it booted|NFS-root: /sbin/ffn-nfsroot'
}

# 1. console broker (single owner of /dev/ttyS1)
python3 tools/ffn_octconsoled.py start 2>/dev/null || true
sleep 1

# 2. boot the OCTEON. We do not probe cpdp first (that would spawn oct-remote);
#    instead, always (re)stage -- ffn_octctl/ffn_octboot are safe to re-run, and
#    on an already-up OCTEON this simply reboots it cleanly into the same flow.
MARK=$(console_mark)
echo "resetting + staging FFN kernel over PCIe (sole oct-remote user)"
python3 tools/ffn_octctl.py boot --dev 0 --force
# mem= is REQUIRED. Without it the kernel takes whatever the OCTEON boot
# descriptor offers, which is ~432 MB of the 8 GB this CP actually has
# (device tree: 0x0+0x10000000 and 0x20000000+0x1F0000000). Measured with
# mem=8G: MemTotal 8150556 kB, an 18x increase.
#
# The suffix matters -- memparse() reads a bare mem=2048 as 2048 BYTES.
# And do not expect to see it in /proc/cmdline afterwards: OCTEON setup.c
# consumes mem= as an early param and strips it, so confirm from the
# console log instead.
python3 tools/ffn_octboot.py --watch 150 --extra "mem=${FFN_MEM:-8G}" &
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
