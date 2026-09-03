#!/bin/bash
# Boot the upstream-6.18 forward-port kernel WITH PCI=y on the PA-5220 CP.
#
# This is the first CP kernel carrying the OCTEON III PCIe port: the SDK's
# cvmx-pcie.c (2590 lines), cvmx-qlm.c, cvmx-qlm-tables.c and the SDK's
# pcie-octeon.c glue, replacing upstream's OCTEON I/II-only inlined copy that
# read MIO_RST_CTL and CIU_SOFT_PRST -- registers absent on CN7XXX -- and took
# a Data bus error in cvmx_pcie_rc_initialize.
#
# Flags identical to boot618.sh so the only variable is PCI:
#   --fdt ""      omit ffn_fdt=  -> __fdt named-block lookup
#   no mem=       -> upstream defaults max_memory to ULLONG_MAX
#   --no-overlay  -> rootfs is EMBEDDED
#   ffn_reserve= x2, repeated form (never ';': u-boot's command separator)
#
# EXPECTED on the host during the reset: a burst of AER CmpltTO errors on
# pcieport 0000:00:01.0 and "device recovery failed" for 01:00.0/.1/.2. That is
# what resetting the OCTEON looks like from the host side while its endpoint is
# mapped -- it happened on every previous boot and is not a new fault. What
# would be new is the endpoint failing to re-enumerate afterwards.
#
# Two things to read on the console that no previous boot could show:
#   1. FFN-INIT lines at all. Earlier boots printed none, because /init had
#      closed stdio -- the kernel cannot open /dev/console from an initramfs
#      that has no console node, and with an initramfs it never calls
#      prepare_namespace(), so CONFIG_DEVTMPFS_MOUNT does not mount /dev
#      either. init now mounts devtmpfs and reopens stdio itself.
#   2. PCI enumeration. host_mode gates cvmx_pcie_rc_initialize per port, so
#      PEM0 -- the endpoint the MP talks to -- must be SKIPPED. If PEM0 gets
#      root-complex initialised, that resets the link this session runs over.
set -u
cd /opt/ffn-ngfw-v2
K=/var/lib/ffn-ngfw/octeon/ffn-vmlinux-6.18.49-cproot
# The kernel this path used to name was -nfs: the build from BEFORE the
# pci-legacy.c busn fix. Booting it printed "PCI host bridge to bus 0002:02 /
# 0003:03" and lost the FE100 and the DP, so the documented boot path was
# staging the broken kernel. Worse, $K was set and then ignored -- the --kernel
# line below carried its own literal, so editing $K changed nothing. One source
# of truth now, and refuse to hand u-boot something that is not a kernel.
if [ ! -s "$K" ]; then echo "ABORT: kernel $K missing or empty"; exit 1; fi
case "$(file -b "$K" 2>/dev/null)" in
	*"ELF 64-bit MSB"*MIPS*) : ;;
	*) echo "ABORT: $K is not a big-endian MIPS64 ELF"; exit 1 ;;
esac
echo "kernel: $K ($(stat -c %s "$K") bytes, md5 $(md5sum "$K" | cut -c1-12))"
LOG=/var/log/ffn-octeon-6.18-pcie-boot.log
# The expected checksum used to be hardcoded here, pinning the OLD -nfs
# kernel. Retargeting $K then made this check fail closed and abort the
# boot -- a checksum that has to be hand-edited in lockstep with a path is
# a trap. It now lives beside the kernel as <kernel>.md5, so staging a new
# image brings its own pin and this script never goes stale.

[ -f "$K" ] || { echo "FAIL staged kernel missing"; exit 1; }
if [ -s "$K.md5" ]; then
	WANT=$(cut -d" " -f1 < "$K.md5")
	md5sum "$K" | grep -q "$WANT" || { echo "FAIL kernel checksum (want $WANT)"; exit 1; }
	echo "kernel checksum verified against $K.md5"
else
	echo "note: no $K.md5 sidecar -- skipping the checksum pin"
fi

# MANDATORY before any OCTEON reset: take the host transport down first.
#
# ffn_pcnetd polls and writes OCTEON DRAM across the BAR window continuously.
# Resetting the OCTEON under it makes every access take a PCIe Completion
# Timeout, and since nothing claims the endpoint the kernel logs
# "AER: can't recover (no error_detected callback)" and retries forever. That
# storm downed this MP on 2026-09-02 and needed a hard power cycle -- see
# journalctl -b -1 around 20:29:03.
#
# Resets with the transport idle are harmless, which is why six of them earlier
# that day were fine; this one followed a 300-packet load test. Do not infer
# safety from the last reset having worked.
#
# tools/pcnet-up.sh brings it back afterwards and reprograms BAR1 index 1, which
# the reset clears regardless.
echo "stopping host ffn-pcnetd before the reset (PCIe CmpltTO/AER hazard)"
systemctl stop ffn-pcnetd 2>/dev/null
for i in 1 2 3 4 5; do
	systemctl is-active --quiet ffn-pcnetd || break
	sleep 1
done
if systemctl is-active --quiet ffn-pcnetd; then
	echo "ABORT: ffn-pcnetd still active; refusing to reset the OCTEON under a live BAR writer"
	exit 1
fi
echo "host ffn-pcnetd stopped"

wc -l < /var/log/ffn-octeon-console.log > /tmp/mark618pcie
echo "console mark: $(cat /tmp/mark618pcie)"

exec >>"$LOG" 2>&1
echo "=== 6.18+PCIe boot attempt $(date) ==="

# Serialise against every other oct-remote user: two at once wedge the serial
# in uninterruptible-D, and a D-state op cannot be killed.
# The INNER heredoc below is QUOTED, so $K is not expanded here, and `bash -s`
# starts a CHILD shell that does not inherit an unexported variable. Under
# set -u that is a hard abort -- "K: unbound variable" -- and it fires AFTER the
# OCTEON has been reset into u-boot but BEFORE the kernel is staged, leaving the
# CP sitting in u-boot with no Linux. Export it; the quoted heredoc still
# protects every other $ in the block from parent expansion.
export K

flock -w 60 /run/ffn-octeon-ctl.lock bash -s <<'INNER'
set -u
cd /opt/ffn-ngfw-v2
echo "--- reset + load u-boot ---"
python3 tools/ffn_octctl.py boot --dev 0 --force
rc=$?
echo "octctl rc=$rc"
[ $rc -eq 0 ] || { echo "ABORT: reset/u-boot stage failed, not attempting the kernel"; exit 1; }

echo "--- stage kernel over the BAR window and boot ---"
python3 tools/ffn_octboot.py \
	--kernel "$K" \
	--no-overlay \
	--fdt "" \
	--extra "ffn_reserve=0x28000000,1M ffn_reserve=0x29000000,4M" \
	--watch 240
echo "octboot rc=$?"
INNER
echo "=== done $(date) ==="
