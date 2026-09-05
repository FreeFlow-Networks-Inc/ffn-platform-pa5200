#!/bin/bash
# Run bcm.user on the OCTEON CP, capturing the transcript ON THE MP.
#
# Everything here exists because of how the first two attempts failed:
#  * The transcript lived on the CP's tmpfs, so the CP reset took it -- both
#    logs were zero bytes and told us nothing.  It now streams over the pcnet
#    link and is written to MP disk as it arrives.
#  * "NMI watchdog" on the console turned out to be what a CP RESET looks like
#    (cores stopped via stop_this_cpu), not a bcm.user fault.  So this records
#    the console offset before and after, and the reset audit log, to tell a
#    genuine hang apart from someone restarting ffn-octeon.service.
#  * A health-probe recovery would itself reset the CP mid-run, so the probe is
#    inhibited for the duration.
set -u
DEADLINE=${1:-420}
SETTLE=${2:-25}
TS=$(date +%Y%m%d-%H%M%S)
LOG=/var/log/ffn-bcm-run-$TS.log
INHIBIT=/run/ffn-cp-health.inhibit
CONSOLE=/var/log/ffn-octeon-console.log
RESETS=/var/log/ffn-octeon-resets.log

cpssh(){ timeout "${CPTMO:-90}" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o ConnectTimeout=15 -o BatchMode=yes -i /root/.ssh/id_ed25519 root@127.1.1.2 "$@" 2>&1 \
  | grep -vE "Warning: Permanently|known hosts"; }

exec > >(tee -a "$LOG") 2>&1
echo "=== ffn-bcm-run-remote $TS  deadline=${DEADLINE}s settle=${SETTLE}s"
echo "=== log: $LOG"

touch "$INHIBIT"; trap 'rm -f "$INHIBIT"' EXIT
echo "=== CP health probe inhibited ($INHIBIT)"

con_off=$(stat -c %s "$CONSOLE" 2>/dev/null || echo 0)
res_off=$(stat -c %s "$RESETS"  2>/dev/null || echo 0)
oct_inv=$(systemctl show -p InvocationID --value ffn-octeon.service 2>/dev/null)
echo "=== before: console=${con_off}B resets=${res_off}B octeon-invocation=$oct_inv"

echo "=== preconditions on the CP"
cpssh "FFN_BCM_THP=${FFN_BCM_THP:-never} sh /opt/ffn/ffn-bcm-prep.sh" || { echo "!!! prep failed rc=$?"; exit 2; }

echo "=== launching bcm.user (transcript streams here, line by line)"
cpssh "python3 -u /opt/ffn/ffn-bcm-run.py $DEADLINE $SETTLE"
rc=$?
echo "=== bcm.user run returned rc=$rc"

echo "=== after: CP health"
bash /opt/ffn-ngfw-v2/tools/ffn-cp-health.sh --status 2>&1 | sed 's/^/  /'
echo "=== console output produced during the run"
tail -c +$(( con_off + 1 )) "$CONSOLE" 2>/dev/null | tr -d '\r' | grep -aE . | tail -40 | sed 's/^/  /'
echo "=== did anything reset the CP during the run?"
new_res=$(tail -c +$(( res_off + 1 )) "$RESETS" 2>/dev/null)
now_inv=$(systemctl show -p InvocationID --value ffn-octeon.service 2>/dev/null)
if [ -n "$new_res" ]; then echo "$new_res" | sed 's/^/  /'
else echo "  no -- ffn-octeon-up.sh was not invoked"; fi
[ "$oct_inv" = "$now_inv" ] && echo "  ffn-octeon.service invocation UNCHANGED (not restarted)" \
                            || echo "  !!! ffn-octeon.service WAS restarted ($oct_inv -> $now_inv)"
echo "=== CP dmesg (BDE/BCM) if the CP is still alive"
cpssh 'dmesg | grep -aE "ffn_bde|ffn_bcm" | tail -25' | sed 's/^/  /'
echo "=== end $(date -Is)"
