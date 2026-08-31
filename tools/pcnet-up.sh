#!/bin/bash
# Bring up the FFN PCIe virtual ethernet host end, cleanly and idempotently.
#   1. free 127.1.1.1 from any stale ffn_pcic pcicp0
#   2. program BAR1 index 1 once (oct-remote-csr) -- SERIALIZED, so it never races
#      the staging path (which also uses oct-remote and would corrupt otherwise)
#   3. start the bridge daemon as a systemd unit with --skip-window
set -u
HERE=$(dirname "$(readlink -f "$0")")
. "$HERE/ffn-octlock.sh"
T=/var/lib/ffn-ngfw/vendor/gryphon-tools/octtools

ip addr flush dev pcicp0 2>/dev/null; ip link set pcicp0 down 2>/dev/null

# The one oct-remote call here must not overlap any other. The lock serialises it
# against a concurrent orchestration (and is a no-op if the orchestration already
# holds it). Programme the window, let the tool fully finish, then RELEASE before
# starting the long-lived daemon so the daemon never carries the lock.
octlock_acquire 300 || exit 1
timeout 20 env LD_LIBRARY_PATH="$T" "$T/oct-remote-csr" spem0_bar1_index1 0xa43 >/dev/null 2>&1
rc=$?
# Wait for THIS invocation to exit (never kill it); a lingering pid is our own
# child finishing, not a reason to hold the lock indefinitely.
wait 2>/dev/null || true
octlock_release
[ "$rc" -eq 0 ] || echo "pcnet-up: oct-remote-csr returned $rc" >&2

systemctl reset-failed ffn-pcnetd 2>/dev/null
systemctl stop ffn-pcnetd 2>/dev/null
exec systemd-run --unit=ffn-pcnetd --no-block \
  /usr/bin/python3 -u /opt/ffn-ngfw-v2/tools/ffn_pcnetd.py --skip-window
