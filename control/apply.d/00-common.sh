#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
#
# Shared helpers for the CP apply hooks. Sourced, not executed -- it has no
# executable bit so ffn_cfgagent's run_hooks() skips it (it only runs files with
# +x), which is exactly how a shared library lives in a run-parts directory.
#
# THE RULE THAT MATTERS MOST
# -------------------------
# The CP's ONLY management path is ffnnet0 (127.1.1.2 <-> the MP's 127.1.1.1),
# a virtual ethernet over PCIe. There is no console on the network, no second
# NIC, and no out-of-band. A hook that flushes addresses or downs a link on that
# interface does not "break networking" -- it removes the only way to fix it,
# and recovery costs a CP reboot driven from the MP.
#
# So every mutating helper here refuses to touch a PROTECTED interface, and the
# protection is applied in the helper rather than left to each hook, because a
# new hook that forgets the check is otherwise one typo away from an outage.
#
# The DP link is protected for the same reason: the CP reaches the DP over it,
# and the DP has no other control channel at all.

# Interfaces no hook may modify, ever. Space separated.
# ffnnet0 : management, CP <-> MP. Losing it loses the CP.
# ffndp0  : CP <-> DP, when dpnet is up. Losing it orphans the dataplane.
# lo      : obvious.
FFN_PROTECTED="${FFN_PROTECTED:-ffnnet0 ffndp0 lo}"

ENVFILE="${1:-/etc/ffn/cp.env}"

log() { echo "  [$(basename "$0")] $*"; }
warn() { echo "  [$(basename "$0")] WARNING: $*" >&2; }

# get <key> [default] -- read one key from the env file.
#
# Uses a literal-prefix grep and cuts at the FIRST '=' so a value containing '='
# survives intact. Values are not shell-evaluated: this file is relayed from the
# MP and a value must never become code here.
get() {
	_v=$(grep -m1 "^$1=" "$ENVFILE" 2>/dev/null | cut -d= -f2-)
	if [ -z "$_v" ]; then echo "${2:-}"; else echo "$_v"; fi
}

# keys_matching <prefix> -- list full keys starting with prefix.
keys_matching() {
	grep -o "^$1[^=]*" "$ENVFILE" 2>/dev/null | sort -u
}

# protected <iface> -- true if this interface must not be touched.
protected() {
	for _p in $FFN_PROTECTED; do
		[ "$1" = "$_p" ] && return 0
	done
	return 1
}

# have <iface> -- true if the interface exists on this box.
have() { ip link show "$1" >/dev/null 2>&1; }

# safe_ip <iface> <ip args...> -- run an ip command against an interface only
# if it is neither protected nor absent. Every mutating hook goes through this.
safe_ip() {
	_if="$1"; shift
	if protected "$_if"; then
		warn "refusing to modify protected interface $_if (management/DP path)"
		return 1
	fi
	if ! have "$_if"; then
		warn "interface $_if does not exist here; skipping"
		return 1
	fi
	ip "$@" || { warn "ip $* failed"; return 1; }
	return 0
}
