#!/bin/sh
# Stage the BCM config the daemon runs on, from the vendor tree plus FFN's
# overrides. Runs ON the CP (busybox sh -- no bash, no pkill, no GNU-only flags).
#
# WHY THIS EXISTS. The chip's entire configuration was a hand-made directory
# nobody could reproduce: a copy of the vendor tree made once by hand, edited by
# hand, with /usr/share/broadcom symlinked at it by hand. Every SOC property
# that took a day to find lived only there, undocumented, one `rm -rf /tmp/*`
# from gone. This makes the working tree a function of two things that ARE in
# git: the vendor firmware, and ffn-bcm-overrides.conf.
#
# THE VENDOR TREE IS NEVER MODIFIED. It is read, copied, and left alone -- that
# is the bring-your-own-firmware rule, and it is also just prudent: the vendor
# files are the only reference for what the appliance originally did.
#
# CWD IS NOT ENOUGH, which is why the destination matters and why the symlink is
# checked. bcm.user carries absolute /usr/share/broadcom paths -- jer.soc writes
# runningConfig.soc back there by absolute path -- so a copy is only really read
# when /usr/share/broadcom points at it AND BCM_CONFIG_FILE names its config.bcm.
# Config changes made without that are silently inert, which cost a day once
# already: the copy said RAW while the chip still reported port_header_type=tm.
set -u

VENDOR=""
DST=/tmp/bcmcfg
OVERRIDES=""
REFRESH=0
DRYRUN=0

usage() {
	cat <<EOF
usage: $0 [--vendor DIR] [--dst DIR] [--overrides FILE] [--refresh] [--dry-run]

  --vendor DIR     vendor broadcom tree to copy from (default: autodetected)
  --dst DIR        working tree to stage into (default: $DST)
  --overrides FILE FFN property overrides (default: alongside this script)
  --refresh        re-copy from the vendor tree even if --dst exists.
                   DISCARDS local edits, including runningConfig.soc.
  --dry-run        say what would change; touch nothing
EOF
}

while [ $# -gt 0 ]; do
	case "$1" in
		--vendor) VENDOR="$2"; shift 2 ;;
		--dst) DST="$2"; shift 2 ;;
		--overrides) OVERRIDES="$2"; shift 2 ;;
		--refresh) REFRESH=1; shift ;;
		--dry-run) DRYRUN=1; shift ;;
		-h|--help) usage; exit 0 ;;
		*) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
	esac
done

if [ -z "$OVERRIDES" ]; then
	OVERRIDES="$(dirname "$0")/ffn-bcm-overrides.conf"
fi

# Autodetect the vendor tree. /usr/share/broadcom is deliberately NOT a
# candidate: on a staged system it is a symlink to $DST, and following it would
# make this script copy the working tree onto itself and call that "the vendor
# config" -- the failure mode where an override silently becomes the baseline.
if [ -z "$VENDOR" ]; then
	# /tmp/dpfs is where the vendor filesystem lands on the CP; /opt/dpfs is
	# the same tree seen from the management plane, which exports it.
	for c in /tmp/dpfs/usr/share/broadcom /opt/dpfs/usr/share/broadcom \
	         /mnt/vendor/usr/share/broadcom /usr/share/broadcom.vendor; do
		if [ -f "$c/config.bcm" ] && [ ! -L "$c" ]; then VENDOR="$c"; break; fi
	done
fi
if [ -z "$VENDOR" ] || [ ! -f "$VENDOR/config.bcm" ]; then
	echo "no vendor broadcom tree found (looked for config.bcm)." >&2
	echo "pass --vendor DIR. The vendor firmware is bring-your-own and is" >&2
	echo "never part of an FFN image, so it must already be mounted." >&2
	exit 1
fi
if [ ! -f "$OVERRIDES" ]; then
	echo "overrides file not found: $OVERRIDES" >&2
	exit 1
fi

# Refuse to treat the destination as its own source.
if [ "$(readlink -f "$VENDOR" 2>/dev/null || echo "$VENDOR")" = \
     "$(readlink -f "$DST" 2>/dev/null || echo "$DST")" ]; then
	echo "--vendor and --dst are the same directory; refusing." >&2
	exit 1
fi

# A --dst that is not absolute is resolved against whatever directory this
# happens to run from. `--dst bcmcfg` from inside the vendor tree would copy
# the vendor firmware into itself -- modifying the one thing this script
# promises never to touch. Require an absolute path rather than guess.
case "$DST" in
	/*) ;;
	*) echo "--dst must be an absolute path, got: $DST" >&2; exit 1 ;;
esac

# `while read` drops a final line with no trailing newline, so an override
# written without one was silently ignored while the script still exited 0.
# Read from a normalised copy instead of trusting the file to end tidily.
OVERRIDES_N="$(mktemp 2>/dev/null || echo /tmp/ffn-ovr.$$)"
trap 'rm -f "$OVERRIDES_N"' EXIT INT TERM
awk '{print}' "$OVERRIDES" > "$OVERRIDES_N" || exit 1

echo "vendor    : $VENDOR"
echo "dest      : $DST"
echo "overrides : $OVERRIDES"

if [ -d "$DST" ] && [ "$REFRESH" -eq 0 ]; then
	echo "staging   : $DST exists, keeping it (use --refresh to re-copy)"
else
	if [ "$DRYRUN" -eq 1 ]; then
		echo "staging   : would copy $VENDOR -> $DST"
	else
		echo "staging   : copying $VENDOR -> $DST"
		rm -rf "$DST" || exit 1
		mkdir -p "$DST" || exit 1
		# `cp -a .` and not `cp -a $VENDOR $DST`: the latter nests a directory
		# inside the destination on some busybox builds.
		( cd "$VENDOR" && cp -a . "$DST"/ ) || exit 1
		chmod -R u+w "$DST" 2>/dev/null
	fi
fi

CFG="$DST/config.bcm"
[ -f "$CFG" ] || { echo "no config.bcm at $CFG after staging" >&2; exit 1; }

# Apply the overrides. Replace in place when the key exists, append when it does
# not -- never both. A duplicated key leaves which value wins up to the SDK's
# parse order, and a property that silently depends on parse order is the kind of
# thing that works until the day it does not.
changed=0
while IFS= read -r line; do
	line="${line%%#*}"
	case "$line" in *[!\ ]*) ;; *) continue ;; esac
	key="${line%%=*}"
	val="${line#*=}"
	key="$(echo "$key" | tr -d ' \t')"
	val="$(echo "$val" | tr -d ' \t')"
	[ -n "$key" ] || continue

	# SOC property names contain '.', which is a regex wildcard. Unescaped,
	# an override for load_firmware.BCM88650 would also match a line reading
	# load_firmware_BCM88650 -- and these files use both separators, so that
	# is a real way to rewrite the wrong property.
	kre="$(echo "$key" | sed 's|[.[]|\\&|g')"

	# Tolerate the spacing the vendor file actually uses. Matching only
	# `^key=` treats a live `key = value` line as absent, appends a second
	# definition, and leaves which one wins up to the SDK's parse order.
	cur="$(grep -E "^[[:space:]]*${kre}[[:space:]]*=" "$CFG" 2>/dev/null | tail -1)"
	cur="${cur#*=}"
	if [ "$cur" = "$val" ]; then
		echo "  ok      $key=$val"
		continue
	fi
	changed=$((changed + 1))
	if [ -n "$cur" ]; then
		echo "  CHANGE  $key: $cur -> $val"
		[ "$DRYRUN" -eq 1 ] || \
			sed -i "s|^[[:space:]]*${kre}[[:space:]]*=.*|${key}=${val}|" 				"$CFG" || exit 1
	else
		echo "  ADD     $key=$val"
		[ "$DRYRUN" -eq 1 ] || {
			printf '\n# added by ffn-bcm-stage-config.sh\n%s=%s\n' \
				"$key" "$val" >> "$CFG" || exit 1
		}
	fi
done < "$OVERRIDES_N"

# Verify what we claim to have done, rather than trusting sed's exit status --
# a mistyped key would otherwise report CHANGE and alter nothing.
# ALWAYS, not only when something changed. The first version ran this only for
# changed>0, which meant: run once over a config.bcm with a duplicated key, get
# "VERIFY FAILED ... appears 2 time(s)" and exit 1 with the tree already
# mutated; run the identical command again, and because `tail -1` now sees the
# wanted value nothing is counted as changed, verification is skipped entirely,
# and it exits 0 reporting success -- with the duplicate still there and the SDK
# free to use whichever line it parses last. A checker that only runs when you
# already believe you changed something cannot catch the state you are in when
# you were wrong.
if [ "$DRYRUN" -eq 0 ]; then
	bad=0
	while IFS= read -r line; do
		line="${line%%#*}"
		case "$line" in *[!\ ]*) ;; *) continue ;; esac
		key="$(echo "${line%%=*}" | tr -d ' \t')"
		val="$(echo "${line#*=}" | tr -d ' \t')"
		[ -n "$key" ] || continue
		kre="$(echo "$key" | sed 's|[.[]|\\&|g')"
		vre="$(echo "$val" | sed 's|[.[*]|\\&|g')"
		got="$(grep -c "^${kre}=${vre}\$" "$CFG" 2>/dev/null)"
		if [ "${got:-0}" -ne 1 ]; then
			echo "  VERIFY FAILED: $key=$val appears $got time(s)" >&2
			bad=$((bad + 1))
		fi
	done < "$OVERRIDES_N"
	[ "$bad" -eq 0 ] || exit 1
fi

# The symlink is half of "the copy is actually read". Report it rather than
# creating it silently -- it points into the vendor tree on an unstaged system
# and clobbering that without saying so would be exactly the wrong surprise.
LNK=/usr/share/broadcom
if [ "$(readlink "$LNK" 2>/dev/null)" = "$DST" ]; then
	echo "symlink   : $LNK -> $DST  (ok)"
else
	echo "symlink   : $LNK is NOT pointing at $DST"
	echo "            bcm.user uses absolute /usr/share/broadcom paths, so the"
	echo "            staged config will NOT be read until it does. Fix with:"
	echo "              mv $LNK $LNK.vendor && ln -s $DST $LNK"
fi

echo "done      : $changed override(s) applied"
