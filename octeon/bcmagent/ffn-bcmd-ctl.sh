#!/bin/sh
# ffn-bcmd-ctl -- start/stop/status for the BCM control daemon. Runs ON the CP.
#
# This exists because the CP's busybox is missing the tools the obvious one-liner
# would use, and each gap cost a debugging cycle:
#
#   * no `pkill`  -- so processes are found with `ps w` + awk and killed by PID.
#     A `pkill -f ffn_bcmd.py` silently does nothing here, which looks exactly
#     like a successful restart until the new daemon dies on EADDRINUSE.
#   * no `nohup`  -- `setsid` alone detaches; `setsid nohup ...` fails with
#     "can't execute 'nohup'".
#   * no `ss`     -- listener checks use `netstat -ln`.
#   * `grep` treats the daemon log as binary, because bcm.user's pty output
#     carries control characters. Use `grep -a`, or `strings`.
#
# And one hardware constraint that shapes the whole script: the BCM88375 is
# SINGLE-SESSION. Stopping the daemon must also kill its bcm.user child, or the
# orphan keeps the chip open and the next start wedges it. `stop` therefore
# kills both, and `start` refuses to run if either is still alive.
set -u

BCMD=/usr/local/ffn/ffn_bcmd.py
# Overridable, because a hardcoded path here is a quiet way to swap the chip's
# firmware out from under a running system: `restart` would stop a daemon using
# one bcm.user and start a different one, with no diagnostic and no obvious
# symptom until some feature that only exists in the other build is missing.
# That nearly happened -- bcm.user.8481 was running while this said .hwswap.
#
# The default is the 8481 build: same tree as .hwswap plus INCLUDE_PHY_8481,
# which adds the phy8481 driver and its firmware (about 2 MB larger, consistent
# with phy8481.o + phy8481_firmware.o). Set FFN_BCM_USER to pin a different one.
BCM=${FFN_BCM_USER:-/usr/local/ffn/bcm.user.8481}
CFG=${FFN_BCM_CFG:-/tmp/bcmcfg}
LOG=/tmp/bcmd.log
PORT=8104

daemon_pids() {
	ps w 2>/dev/null | grep -a "[ /]ffn_bcmd\.py" | grep -av ffn-bcmd-ctl \
		| awk '{print $1}'
}

bcmuser_pids() {
	# The daemon's OWN command line contains "--bcm .../bcm.user.hwswap", so a
	# naive grep for bcm.user matches the daemon too. That made `status` list the
	# daemon as a bcm.user and would have made `start`'s single-session guard
	# refuse to start every time. Exclude the daemon's line explicitly.
	ps w 2>/dev/null | grep -a "bcm\.user" \
		| grep -av ffn_bcmd\.py | grep -av ffn-bcmd-ctl \
		| awk '{print $1}'
}

status() {
	D=$(daemon_pids | tr '\n' ' ')
	B=$(bcmuser_pids | tr '\n' ' ')
	L=$(netstat -ln 2>/dev/null | grep -ac ":$PORT ")
	echo "  daemon pids : ${D:-none}"
	echo "  bcm.user    : ${B:-none}"
	echo "  listening   : $([ "${L:-0}" -gt 0 ] && echo yes || echo no) (port $PORT)"
	[ -f "$LOG" ] && echo "  last log    : $(strings "$LOG" 2>/dev/null | tail -1)"
	# An orphaned bcm.user with no daemon is the state that wedges the chip, so
	# call it out rather than leaving it to be read off the two lines above.
	if [ -z "$D" ] && [ -n "$B" ]; then
		echo "  WARNING: bcm.user is running with no daemon -- it holds the"
		echo "           single-session chip. Run '$0 stop' before starting."
	fi
}

stop() {
	for p in $(daemon_pids); do kill "$p" 2>/dev/null && echo "  TERM daemon $p"; done
	sleep 2
	# The daemon reaps its own child on SIGTERM; anything left is an orphan.
	for p in $(bcmuser_pids); do kill "$p" 2>/dev/null && echo "  TERM bcm.user $p"; done
	sleep 1
	for p in $(daemon_pids) $(bcmuser_pids); do
		kill -9 "$p" 2>/dev/null && echo "  KILL $p (did not exit on TERM)"
	done
	sleep 1
}

start() {
	if [ -n "$(daemon_pids)" ]; then
		echo "  already running; use restart"; return 1
	fi
	if [ -n "$(bcmuser_pids)" ]; then
		echo "  refusing to start: a bcm.user is already running and the chip is"
		echo "  single-session. Run '$0 stop' first."; return 1
	fi
	[ -x "$BCM" ] || { echo "  bcm.user not executable: $BCM"; return 2; }
	[ -d "$CFG" ] || { echo "  config dir missing: $CFG (run ffn-bcm-prep.sh)"; return 2; }
	cd /tmp || return 2
	# Say which binary, every time. The whole point of the override above is
	# that WHICH bcm.user is running matters and is otherwise invisible.
	echo "  starting with $BCM ($(stat -c %s "$BCM" 2>/dev/null) bytes)"
	setsid python3 "$BCMD" --bcm "$BCM" --cfg "$CFG" \
		> /tmp/bcmd.out 2> "$LOG" < /dev/null &
	sleep 5
	status
	echo "  NOTE: chip init takes ~50 s warm, ~150 s cold. Poll"
	echo "        {\"op\":\"status\"} until state becomes \"ready\"."
}

case "${1:-status}" in
start)   start ;;
stop)    stop; status ;;
restart) stop; start ;;
status)  status ;;
*)       echo "usage: $0 {start|stop|restart|status}"; exit 2 ;;
esac
