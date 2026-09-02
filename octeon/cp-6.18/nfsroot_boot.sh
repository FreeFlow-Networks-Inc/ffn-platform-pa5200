#!/bin/bash
# Orchestrate the NFS-root boot attempt, with the sequencing that was missing
# when this downed the MP earlier.
#
# Three rules, each from a specific failure:
#
#  1. boot618-pcie.sh stops ffn-pcnetd before the reset. Resetting the OCTEON
#     while the host daemon drives the BAR window produces PCIe CmpltTO ->
#     "AER: can't recover (no error_detected callback)" -> root port down.
#     The script aborts if the daemon will not stop, so we do not have to trust
#     this comment.
#
#  2. WAIT for the boot to actually finish before touching the vendor tools
#     again -- by polling for its own "=== done" marker, not by sleeping a
#     guessed number of seconds. The boot holds /run/ffn-octeon-ctl.lock for its
#     whole --watch 240 window; a fixed sleep is what put the two in contention.
#
#  3. Run pcnet-up.sh with NO external `timeout`. It says "never kill it" about
#     its oct-remote-csr child, and it already bounds that call internally with
#     `timeout 20`. Wrapping it externally is how a vendor PCIe tool gets killed
#     mid-transaction.
set -u
LOG=/var/log/ffn-octeon-6.18-pcie-boot.log
OUT=/tmp/nfsroot-attempt.log
exec > "$OUT" 2>&1

echo "=== attempt starting $(date) ==="
BEFORE=$(wc -l < "$LOG" 2>/dev/null || echo 0)

echo "--- launching boot (it stops ffn-pcnetd itself) ---"
bash /root/boot618-pcie.sh &
BOOTPID=$!

# Poll for completion rather than sleeping a guess. 420s ceiling covers the
# reset + u-boot + 27MB stage + --watch 240 with room to spare.
echo "--- waiting for the boot to finish (polling its own done marker) ---"
done=0
for i in $(seq 1 140); do
	if tail -n +$((BEFORE+1)) "$LOG" 2>/dev/null | grep -q "=== done"; then
		done=1; echo "boot script reported done after ~$((i*3))s"; break
	fi
	if ! kill -0 "$BOOTPID" 2>/dev/null; then
		done=1; echo "boot script exited after ~$((i*3))s"; break
	fi
	sleep 3
done
[ "$done" = 1 ] || echo "WARNING: boot did not report done within 420s; NOT touching oct-remote"

echo "--- boot log tail ---"
tail -n +$((BEFORE+1)) "$LOG" | tail -12

if [ "$done" = 1 ]; then
	echo "--- bringing the host end up (no external timeout) ---"
	cd /opt/ffn-ngfw-v2 && bash tools/pcnet-up.sh
	echo "pcnet-up rc=$?"
else
	echo "--- SKIPPED pcnet-up: boot never confirmed done ---"
fi

echo "=== attempt finished $(date) ==="
