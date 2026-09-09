#!/bin/sh
# Boot the DP on the NFS-root kernel, attempt 2. Runs ON THE CP.
#
# WHAT CHANGED SINCE ATTEMPT 1
#
#  * The DP's init now starts /sbin/ffn_dpagent2 first, in a child, so the
#    mailbox agent is live before the nfsroot flow runs. Attempt 1's init had
#    no agent branch at all and the DP came up unreachable.
#
#  * We no longer sleep a fixed 45s before starting the CP's dpnet end. We
#    POLL FOR THE AGENT instead, because that is the actual precondition:
#    ffn_dpnetd refuses to start until it can read the agent's magic
#    0x46464e4450534832 at BAR offset 0x400000, and `ffn-dpsh --status` is
#    what programs the BAR window and toggles enable in the first place.
#    Polling a real signal beats guessing a duration -- the same lesson
#    nfsroot_boot.sh already records for the CP boot.
#
#  * setsid, not nohup: the CP's busybox has no nohup applet. Attempt 1
#    printed "nohup: not found", never booted, and still reported success
#    because it only checked that a pid existed.
#
# The DP's script waits up to 60s for the CP and restarts its own dpnetd if it
# times out, so a slow CP end is survivable rather than fatal.
set -u

FFN=/opt/ffn
K=$FFN/ffn-vmlinux-6.18.49-dp-nfsroot
DPSH=/usr/local/bin/ffn-dpsh
DPNETD=/usr/local/bin/ffn_dpnetd
LOG=/tmp/dp-nfsboot2.log

say() { echo "nfsboot2: $*" | tee -a "$LOG"; }
: > "$LOG"

[ -e "$K" ] || { say "kernel missing: $K"; exit 1; }
say "kernel: $K ($(stat -c %s "$K") bytes)"

say "1. stopping the CP end of dpnet before the DP reset"
# By pid: pkill -f with a pattern that also appears in this script's own argv
# kills the caller. That has already happened twice.
for p in $(pidof ffn_dpnetd 2>/dev/null); do
	kill "$p" 2>/dev/null && say "   killed pid $p"
done
sleep 2
if pidof ffn_dpnetd >/dev/null 2>&1; then
	say "   still running -- refusing to reset the DP under it"
	exit 2
fi
say "   stopped"

say "2. booting the DP"
FFN_DP_KERNEL="$K" setsid sh "$FFN/dpboot8.sh" >/tmp/dpboot8-a2.log 2>&1 </dev/null &
BOOTPID=$!
sleep 5
if ! kill -0 "$BOOTPID" 2>/dev/null && [ ! -s /tmp/dpboot8-a2.log ]; then
	say "   dpboot8 never started:"
	cat /tmp/dpboot8-a2.log 2>/dev/null | sed 's/^/     /'
	exit 3
fi
say "   dpboot8 running (pid $BOOTPID)"

say "3. polling for the DP session agent (the precondition for dpnet)"
agent=0
i=0
while [ "$i" -lt 40 ]; do
	S=$("$DPSH" --status 2>&1)
	case "$S" in
	*"agent v2"*)
		agent=1
		say "   agent up after ~$((i * 5))s: $(echo "$S" | head -1)"
		break
		;;
	esac
	i=$((i + 1))
	sleep 5
done
if [ "$agent" != 1 ]; then
	say "   NO AGENT after 200s -- the reconstruction did not take"
	say "   last status: ${S:-none}"
	say "   RECOVER WITH:"
	say "     FFN_DP_KERNEL=$FFN/ffn-vmlinux-6.18.49-dp-pknd4 sh $FFN/dpboot8.sh"
	exit 4
fi

say "4. starting the CP end of dpnet"
setsid "$DPNETD" --role cp -v >/tmp/dpnetd-cp.log 2>&1 </dev/null &
sleep 5
if pidof ffn_dpnetd >/dev/null 2>&1; then
	say "   up (pid $(pidof ffn_dpnetd))"
else
	say "   FAILED to start:"
	tail -5 /tmp/dpnetd-cp.log 2>/dev/null | sed 's/^/     /'
fi

say "5. waiting for the DP to mount and switch (its window is 60s)"
i=0
while [ "$i" -lt 30 ]; do
	if ping -c1 -W1 127.1.2.2 >/dev/null 2>&1; then
		say "   DP answers on 127.1.2.2 after ~$((i * 3))s"
		break
	fi
	i=$((i + 1))
	sleep 3
done

say "6. what is the DP's root now?"
"$DPSH" -c 'grep -m2 " / " /proc/mounts; echo ---; ls /oldroot 2>/dev/null | head -3; echo ---; cat /etc/openwrt_release 2>/dev/null | head -2' -t 90 2>&1 \
	| grep -v '@@' | sed 's/^/     /' | tee -a "$LOG"

say "--- dpboot8 tail ---"
tail -12 /tmp/dpboot8-a2.log | sed 's/^/     /'
