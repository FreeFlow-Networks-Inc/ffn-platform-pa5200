# ffn-octlock.sh -- serialize everything that touches the vendor oct-remote-*
# tools (reset/boot/load/csr) and the PCIe staging window.
#
# WHY: oct-remote-* wedge the OCTEON serial in uninterruptible-D state if two run
# at once or one is killed mid-op, which then presents as `sha256 MISMATCH`
# staging failures. A D-state process cannot be killed, so the ONLY safe response
# is to serialize (one at a time) and to WAIT for a busy one to finish -- never to
# kill it. This file is sourced by pcnet-up.sh and ffn-octeon-up.sh.
#
# Usage:
#   . ffn-octlock.sh
#   octlock_acquire 300        # blocks up to Ns for the global lock; no-op if a
#                              # parent already holds it (FFN_OCTEON_LOCKED=1)
#   octremote_wait_idle 120    # wait for any in-flight oct-remote to finish
#   ... oct-remote work ...
#   octlock_release            # drop the lock before exec'ing a long-lived daemon

FFN_OCTLOCK_FILE=/run/ffn-octeon-ctl.lock
FFN_OCTLOCK_FD=9

octlock_acquire() {
	# A parent already holding the lock passes FFN_OCTEON_LOCKED=1 to us; do not
	# take it again (flock is not recursive and we would deadlock).
	if [ "${FFN_OCTEON_LOCKED:-}" = 1 ]; then
		return 0
	fi
	eval "exec ${FFN_OCTLOCK_FD}>\"$FFN_OCTLOCK_FILE\""
	if ! flock -w "${1:-300}" "$FFN_OCTLOCK_FD"; then
		echo "octlock: could not acquire $FFN_OCTLOCK_FILE within ${1:-300}s" >&2
		return 1
	fi
	# children (e.g. pcnet-up called from the orchestration) inherit the hold
	export FFN_OCTEON_LOCKED=1
	FFN_OCTLOCK_HELD=1
	return 0
}

octlock_release() {
	# Only release if WE took it (not if a parent owns it). Closing the fd drops
	# the flock. Important before exec'ing a daemon so the daemon does not carry
	# the lock for its whole life.
	if [ "${FFN_OCTLOCK_HELD:-}" = 1 ]; then
		eval "exec ${FFN_OCTLOCK_FD}>&-"
		unset FFN_OCTEON_LOCKED FFN_OCTLOCK_HELD
	fi
}

octremote_wait_idle() {
	# Wait for any running oct-remote-* to finish. NEVER pkill: they can be in
	# uninterruptible-D on the serial and killing leaves the wedge, not clears it.
	local t="${1:-120}" i=0
	while [ "$(pgrep -cf 'oct-remote' 2>/dev/null || echo 0)" != 0 ]; do
		i=$((i + 1))
		if [ "$i" -ge "$t" ]; then
			echo "octremote: still busy after ${t}s (a D-state op is stuck); refusing to proceed" >&2
			return 1
		fi
		sleep 1
	done
	return 0
}
