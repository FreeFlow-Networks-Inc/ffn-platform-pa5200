#!/bin/bash
# ffn-dpnet-up6 -- bring up the CP<->DP virtual Ethernet from a 6.18 CP.
#
# Runs ON the CP. octeon/dpnet2/ffn-dpnet-up.sh does the same job but drives
# both ends indirectly from the MP through ffn_cpsh, and assumes the 4.9 CP's
# vendor root: it calls `/usr/bin/python`, which does not exist here (this root
# has python3 only). Running on the CP removes a whole layer of quoting and the
# MP-side timeout wrappers.
#
# The daemon is ONE static big-endian MIPS64 binary that implements both ends
# (--role cp / --role dp). Pure userspace: no kernel module on either side, so
# nothing here needs rebuilding for 6.18. ffn_dpnet-NUMACONFIG-ONLY.ko is
# vermagic 4.9.57 and must NEVER be force-loaded.
#
# Delivery: the DP's initramfs has no ffn_dpnetd (its /sbin is just ffn-dproot,
# ffn_bcmctl, ffn_cpdpd, ffn_dpagent, ffn_dpagent2) and the agent shell has no
# file-push. So the CP writes the binary into DP DRAM through the BAR window and
# the DP dd's it back out of /dev/mem, with a sha256 check on arrival.
#
# The staging address is safe by construction, not by luck: ffn_dpstage.py uses
# DP phys 0x500000, deliberately clear of the agent mailbox at 0x400000 and the
# dpnet rings at 0x600000 -- and the DP was booted with ffn_reserve=0x400000,4M,
# so the whole 0x400000-0x800000 window is out of the DP kernel's allocator.
# Staging anywhere else (0x28000000, say) would land in managed DRAM under
# mem=30G and be corrupted or corrupt something else.
set -u

BIN=/usr/local/bin/ffn_dpnetd
STAGE=/usr/local/bin/ffn_dpstage.py
DPSH=/usr/local/bin/ffn-dpsh
DP_TMP=/tmp/ffn_dpnetd
STAGE_MB=5                      # DP phys 0x500000
LOG=${LOG:-/tmp/dpnet-up6.log}

say(){ echo "dpnet-up6: $*" | tee -a "$LOG"; }
dp(){ $DPSH -t 120 -c "$1" 2>&1; }
: > "$LOG"

for f in "$BIN" "$STAGE" "$DPSH"; do
	[ -s "$f" ] || { say "FATAL: missing $f"; exit 1; }
done

# --- 0. the DP must actually be up, or staging writes into nothing ----------
S=$($DPSH --status 2>&1 | head -1)
case "$S" in
	*"agent v2"*) say "DP agent: $S" ;;
	*) say "FATAL: no DP agent session ($S) -- boot the DP first (dpboot8.sh)"; exit 1 ;;
esac

BYTES=$(wc -c < "$BIN")
WANT=$(sha256sum "$BIN" | cut -d' ' -f1)
BLOCKS=$(( (BYTES + 1048575) / 1048576 ))
say "daemon $BYTES bytes, sha256 ${WANT:0:16}..., $BLOCKS MiB block(s)"

# --- 1. push the binary into DP DRAM ---------------------------------------
say "1. staging into DP DRAM at ${STAGE_MB}MB (0x500000)"
/usr/bin/python3 "$STAGE" "$BIN" >>"$LOG" 2>&1
say "   stage rc=$?"

# --- 2. DP reads it back out of /dev/mem -----------------------------------
# Split across calls rather than one long line: ffn-dpsh echoes through a pty
# that wraps long lines, and a wrapped line comes back mangled.
say "2. DP reading it out of /dev/mem"
dp "dd if=/dev/mem of=/tmp/dpnetd.raw bs=1M skip=$STAGE_MB count=$BLOCKS 2>/dev/null; echo dd=\$?" >>"$LOG" 2>&1
dp "head -c $BYTES /tmp/dpnetd.raw > $DP_TMP.new; chmod 755 $DP_TMP.new; mv -f $DP_TMP.new $DP_TMP; rm -f /tmp/dpnetd.raw; echo mv=\$?" >>"$LOG" 2>&1
H=$(dp "sha256sum $DP_TMP | cut -c1-16")
case "$H" in
	*"${WANT:0:16}"*) say "   sha256 matches on the DP: ${WANT:0:16}" ;;
	*) say "FATAL: DP copy hash mismatch (got: $H, want ${WANT:0:16})"; exit 1 ;;
esac

# --- 3. CP end first: it builds the region header the DP waits for ---------
say "3. starting the CP end"
kill -TERM $(pidof ffn_dpnetd) 2>/dev/null
sleep 2; kill -9 $(pidof ffn_dpnetd) 2>/dev/null; sleep 1
setsid "$BIN" --role cp -v > /tmp/dpnetd-cp.log 2>&1 < /dev/null &
for i in $(seq 1 25); do grep -q "side running" /tmp/dpnetd-cp.log 2>/dev/null && break; sleep 1; done
grep -q "side running" /tmp/dpnetd-cp.log || { say "FATAL: CP end never came up"; tail -6 /tmp/dpnetd-cp.log | tee -a "$LOG"; exit 1; }
say "   $(grep -m1 'side running' /tmp/dpnetd-cp.log)"

# --- 4. then the DP end -----------------------------------------------------
say "4. starting the DP end"
dp "kill -TERM \$(pidof ffn_dpnetd) 2>/dev/null; sleep 1; kill -9 \$(pidof ffn_dpnetd) 2>/dev/null; echo killed" >>"$LOG" 2>&1
dp "setsid $DP_TMP --role dp -v --wait 30 > /tmp/dpnetd-dp.log 2>&1 < /dev/null & echo started" >>"$LOG" 2>&1
sleep 8
DPL=$(dp "tail -4 /tmp/dpnetd-dp.log")
echo "$DPL" >> "$LOG"
case "$DPL" in *"side running"*) say "   DP end running" ;; *) say "   DP end status unclear, see $LOG" ;; esac

# --- 5. prove it carries traffic -------------------------------------------
say "5. verifying"
ip -4 -o addr show ffndp0 2>/dev/null | tee -a "$LOG"
if ping -c 3 -W 2 127.1.2.2 >/dev/null 2>&1; then
	say "=== CP<->DP NETWORK UP: ping 127.1.2.2 OK ==="
	"$BIN" --role cp --status 2>&1 | head -8 | tee -a "$LOG"
	exit 0
fi
say "ping 127.1.2.2 failed; region state:"
"$BIN" --role cp --status 2>&1 | head -8 | tee -a "$LOG"
exit 2
