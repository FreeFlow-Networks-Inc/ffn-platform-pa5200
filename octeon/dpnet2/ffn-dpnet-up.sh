#!/bin/bash
# ffn-dpnet-up -- bring up the CP <-> DP virtual Ethernet, end to end.
#
# Runs on the MP. Drives the CP through ffn_cpsh (pcnet) and the DP through
# ffn-dpsh (the PCIe shell channel), so it needs no console and no DP network --
# which is the point, since this script is what GIVES the DP its network.
#
# What it does, in order:
#   1. check the MP <-> CP pcnet link is up (everything below rides on it)
#   2. push the daemon to the CP's NFS rootfs
#   3. stage the daemon into DP DRAM over PCIe and have the DP dd it back out
#   4. start the CP end (it owns region init), then the DP end
#   5. add the transit routes so MP <-> DP works through the CP
#   6. verify with ping from both the CP and the MP
#
# Idempotent: safe to re-run. Restarting the CP end bumps the region generation
# and zeroes the ring counters, and the DP end reads those live, so the two
# resynchronise without a handshake.
#
# All addresses are in 127/8, which is non-routable: this link cannot be reached
# from any physical topology, only across PCIe.
set -u

CPSH="python3 /opt/ffn-ngfw-v2/tools/ffn_cpsh.py"
BIN_SRC=/opt/dpfs/usr/local/bin/ffn_dpnetd     # CP sees this as /usr/local/bin
STAGE_MB=5                                     # DP phys 0x500000
DP_TMP=/tmp/ffn_dpnetd

CP_ADDR=127.1.2.1
DP_ADDR=127.1.2.2
MP_ADDR=127.1.1.1
CP_PCNET=127.1.1.2

say()  { echo "== $*"; }
fail() { echo "!! $*" >&2; exit 1; }

# Run one command on the CP. Deliberately one line: ffn_cpsh -c sends the string
# to a shell and waits for an end marker, and embedded newlines break that.
cp_run()  { timeout 180 $CPSH -t 150 -c "$1"; }
# Run one command on the DP, through the CP.
# Run one command on the DP, through the CP. The inner command is SINGLE-quoted
# because it passes through three shells: MP bash, then the CP shell, then the
# DP shell. Double-quoting it there would make the CP shell perform any command
# substitution inside it -- so "kill $(pidof ...)" sent to the DP would carry
# the CP's pid list, kill the wrong processes, and leave the DP's daemon holding
# /dev/net/tun. Single quotes stop the CP shell touching it at all.
# Consequence: $1 must not contain a single quote.
dp_run()  { timeout 200 $CPSH -t 170 -c "ffn-dpsh -t 120 -c '$1'"; }

# ---------------------------------------------------------------- 1. pcnet ---
say "checking the MP <-> CP pcnet link"
ip -br addr show ffnnet0 >/dev/null 2>&1 || fail "ffnnet0 is missing -- start ffn_pcnetd.py on the MP first"
ping -c1 -W3 "$CP_PCNET" >/dev/null 2>&1 || fail "no reply from the CP at $CP_PCNET -- fix pcnet before dpnet"

# ------------------------------------------------------------------ 2. bin ---
[ -x "$BIN_SRC" ] || fail "$BIN_SRC missing -- build it on the build server and copy it here"
WANT=$(sha256sum "$BIN_SRC" | cut -d" " -f1)
say "daemon $WANT"

# ---------------------------------------------------------------- 3. stage ---
# The DP needs the binary before it has a network to fetch it over, so the CP
# writes it into DP DRAM through the BAR1 window and the DP reads it back.
say "staging the daemon into DP DRAM at ${STAGE_MB}MB"
BYTES=$(stat -c %s "$BIN_SRC")
cp_run "/usr/bin/python /usr/local/bin/ffn_dpstage.py /usr/local/bin/ffn_dpnetd" \
	| grep -E "readback|bytes|address" || fail "staging failed"

say "extracting it on the DP"
BLOCKS=$(( (BYTES + 1048575) / 1048576 ))
# Write to a new name and rename over the old one. Writing directly would fail
# with ETXTBSY whenever a previous DP daemon is still running from that path --
# rename is allowed, and the running process keeps the old inode until it exits.
#
# Verification prints a short token rather than being grepped for the hash
# itself: ffn-dpsh echoes through a pty that wraps long lines, and a wrapped
# 64-character hash would never match.
DPX=$(dp_run "dd if=/dev/mem of=/tmp/dpnetd.raw bs=1M skip=$STAGE_MB count=$BLOCKS 2>/dev/null; head -c $BYTES /tmp/dpnetd.raw > ${DP_TMP}.new; chmod 755 ${DP_TMP}.new; rm -f /tmp/dpnetd.raw; mv -f ${DP_TMP}.new $DP_TMP; if sha256sum $DP_TMP | grep -q $WANT; then echo DPHASH-OK; else echo DPHASH-BAD; fi" 2>&1)
if ! printf '%s' "$DPX" | tr -d '[:space:]' | grep -q DPHASH-OK; then
	echo "--- DP output ---" >&2; echo "$DPX" | tail -20 >&2
	fail "the DP copy does not verify as $WANT"
fi
say "DP copy verified"

# ------------------------------------------------------------------ 4. run ---
# CP first: it owns region init, and the DP end waits for the header.
say "restarting the CP end"
CPX=$(cp_run "kill -TERM \$(pidof ffn_dpnetd) 2>/dev/null; for i in 1 2 3 4 5 6 7 8 9 10; do ip link show ffndp0 >/dev/null 2>&1 || break; sleep 1; done; kill -9 \$(pidof ffn_dpnetd) 2>/dev/null; sleep 1; setsid /usr/local/bin/ffn_dpnetd --role cp -v > /tmp/dpnetd-cp.log 2>&1 < /dev/null & for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do grep -q "side running" /tmp/dpnetd-cp.log 2>/dev/null && break; sleep 1; done; tail -4 /tmp/dpnetd-cp.log" 2>&1)
echo "$CPX" | grep -E "region initialised|up at|running" || true
if ! printf '%s' "$CPX" | tr -d '[:space:]' | grep -q "CPsiderunning"; then
	echo "$CPX" | tail -20 >&2; fail "the CP end did not start"
fi

say "restarting the DP end"
DPY=$(dp_run "kill -TERM \$(pidof ffn_dpnetd) 2>/dev/null; for i in 1 2 3 4 5 6 7 8 9 10; do ip link show ffndp0 >/dev/null 2>&1 || break; sleep 1; done; kill -9 \$(pidof ffn_dpnetd) 2>/dev/null; sleep 1; setsid $DP_TMP --role dp -v --wait 30 > /tmp/dpnetd-dp.log 2>&1 < /dev/null & for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do grep -q "side running" /tmp/dpnetd-dp.log 2>/dev/null && break; sleep 1; done; tail -4 /tmp/dpnetd-dp.log" 2>&1)
echo "$DPY" | grep -E "attached to CP|up at|running" || true
# EBUSY here means the previous daemon had not released /dev/net/tun yet -- the
# failure that let an old binary keep serving the link unnoticed.
if ! printf '%s' "$DPY" | tr -d '[:space:]' | grep -q "DPsiderunning"; then
	echo "$DPY" | tail -20 >&2; fail "the DP end did not start"
fi

# --------------------------------------------------------------- 5. routes ---
# The CP forwards between the two 127/8 links. route_localnet is what lets a
# 127/8 address live on a non-loopback interface at all.
say "routing MP <-> DP through the CP"
cp_run "echo 1 > /proc/sys/net/ipv4/ip_forward; echo 1 > /proc/sys/net/ipv4/conf/ffnnet0/route_localnet; echo 1 > /proc/sys/net/ipv4/conf/ffndp0/route_localnet; echo forwarding=\$(cat /proc/sys/net/ipv4/ip_forward)"

# The CP's eth0 must not carry the dpnet address: it is a physical port, it
# would win the route over ffndp0 (lower ifindex), and the CP is supposed to
# hold no IP on eth0/eth1 at all. Seen in the wild, hence the explicit removal.
cp_run "ip addr del ${CP_ADDR}/24 dev eth0 2>/dev/null; ip route get $DP_ADDR"

dp_run "ip route add 127.1.1.0/24 via $CP_ADDR dev ffndp0 2>/dev/null; ip route show | grep 127.1.1"

sysctl -qw net.ipv4.conf.ffnnet0.route_localnet=1 2>/dev/null
ip route replace 127.1.2.0/24 via "$CP_PCNET" dev ffnnet0 src "$MP_ADDR" \
	|| fail "could not add the MP transit route"

# --------------------------------------------------------------- 6. verify ---
say "CP -> DP"
cp_run "ping -c3 -W3 $DP_ADDR 2>&1 | tail -3" | tee /tmp/dpnet-cp-ping.txt
grep -q " 0% packet loss" /tmp/dpnet-cp-ping.txt || fail "CP cannot reach the DP"

say "MP -> DP (two PCIe hops)"
ping -c3 -W4 "$DP_ADDR" 2>&1 | tail -3 || fail "MP cannot reach the DP"

say "ring state"
cp_run "/usr/local/bin/ffn_dpnetd --role cp --status"

say "up: MP $MP_ADDR <-> CP $CP_PCNET | $CP_ADDR <-> DP $DP_ADDR"
