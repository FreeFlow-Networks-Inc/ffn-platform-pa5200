#!/bin/sh
# FFN: bring the BCM88375 up to the vendor SDK prompt through FFN's own BDE.
#
# Everything the CP holds lives in RAM or devtmpfs and is destroyed whenever the
# Octeon is re-booted, so this script is idempotent and re-runnable: it loads
# ffn_bde only if absent, creates the device nodes only if absent, then drives
# bcm.user from a command file.
#
# Usage: ffn-bcm-sdk.sh <cmdfile> [logfile]
set -u
CMDS="${1:-/tmp/bcm-cmds.txt}"
LOG="${2:-/tmp/bcm-run.log}"
KO=/tmp/ffn_bde.ko

# 0. Refuse to insmod onto a degraded PCIe path.
#
#    A Data Bus Error on the BCM88375 leaves the OCTEON PCIe path in an
#    error state in which even BAR0+0x2030 -- which always reads -- faults.
#    ffn_bde reads exactly that at probe, from the kernel, so insmod onto a
#    degraded path is a kernel OOPS, not a failed load. Observed 2026-09-01:
#    a run hit the bcmINTR DBE, and the next insmod oopsed in
#    ffn_bde_paxb_init on the 0x2030 readback.
#
#    An already-loaded module is fine -- the fault only bites at probe, so
#    this guard only applies when we would actually load. Recovery is
#    software: systemctl restart ffn-octeon.service on the MP.
if ! grep -q '^ffn_bde ' /proc/modules; then
	if dmesg | grep -q 'Data bus error'; then
		echo "REFUSING to insmod: dmesg shows a Data bus error, so the"
		echo "PCIe path to the BCM is degraded and probe would oops it."
		echo "Re-boot the CP: systemctl restart ffn-octeon.service"
		exit 1
	fi
	# paxb_full=1 and the dma_hi_bits derivation are the build defaults now; the
	# pool is the ffn_reserve= range on the CP boot line (ffn-octeon-up.sh), which
	# is why it is named here rather than baked into the driver.
	insmod "$KO" dma_phys=0x30000000 dma_mb=64 || { echo "insmod $KO failed"; exit 1; }
fi

# 2. device nodes. /dev is devtmpfs, so these do NOT survive a CP re-boot; the
#    vendor's own nodes in the NFS root are masked by that mount.
[ -c /dev/linux-kernel-bde ] || mknod /dev/linux-kernel-bde c 127 0
[ -c /dev/linux-user-bde ]   || mknod /dev/linux-user-bde   c 126 0

# 3. bcm.user shells out to modprobe for the vendor BDE modules and prints a
#    FATAL when modules.dep is missing. It is not fatal -- it proceeds to open
#    the nodes above -- but depmod removes the misleading line.
[ -f /lib/modules/$(uname -r)/modules.dep ] || depmod -a 2>/dev/null

# 4. cores. Both of these are reset by every CP re-boot, so set them here rather
#    than by hand: without them a crash leaves nothing to analyse, and analysing
#    a core from an earlier run with different module params is worse than none.
ulimit -c unlimited
echo "/tmp/core.%e.%p" > /proc/sys/kernel/core_pattern 2>/dev/null

cd /usr/share/broadcom || exit 1
{
	echo "=== ffn-bcm-sdk $(date) ==="
	echo "--- uptime: $(cat /proc/uptime) ---"
	echo "--- module: $(grep '^ffn_bde ' /proc/modules) ---"
	echo "--- nodes: $(ls -l /dev/linux-kernel-bde /dev/linux-user-bde 2>&1) ---"
	echo "--- cwd: $(pwd) ---"
	echo "--- cmds ---"
	cat "$CMDS"
	echo "--- output ---"
} > "$LOG"

# 300s ceiling: init soc is long, but a wedged run must not hold the box.
timeout 300 /usr/local/cp/bcm.user < "$CMDS" >> "$LOG" 2>&1
echo "=== exit=$? $(date) ===" >> "$LOG"
