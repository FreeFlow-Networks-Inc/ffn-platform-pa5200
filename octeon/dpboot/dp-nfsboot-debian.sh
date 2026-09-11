#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
#
# Boot the DP on its NFS-root kernel FROM A DEBIAN CONTROL PLANE. Runs ON THE CP.
#
# This is dp-nfsboot.sh ported to the CP's move from OpenWrt to Debian. The
# layering, the ordering and the recovery path are unchanged -- see
# ../DP-NFSROOT.md and ../NFS-LAYERING.md -- but every path the old script used
# now lives on the other side of a chroot, and three of the tools it called are
# not installed on a Debian root at all.
#
# ---------------------------------------------------------------------------
# WHAT MOVED, AND WHY EACH ONE MATTERS
# ---------------------------------------------------------------------------
#
# THE BOOT TOOLING IS IN THE COMPAT ROOT. dpboot8.sh wants $VT=/tmp/dpfs (the
# vendor tree, NFS-mounted read-only and used in place, never packaged),
# $FFN=/opt/ffn (the staged kernels) and a glibc loader at /lib/ld.so.1 for the
# vendor oct-remote-* binaries. On the Debian CP none of those resolve: /opt
# holds only ffn-compat. They all resolve INSIDE /opt/ffn-compat, which is the
# old OpenWrt root NFS-mounted from the MP with proc, sys and dev already bound
# in. So dpboot8 is run under `chroot /opt/ffn-compat`, exactly the way
# ffn-bcm-debian.sh runs the BCM agent. Every path dpboot8 prints is therefore
# chroot-relative; /tmp/dpboot8.log means /opt/ffn-compat/tmp/dpboot8.log here,
# and /opt/ffn-cproot-owrt/tmp/dpboot8.log on the MP.
#
# ffn_dpnetd RUNS FROM tmpfs, NOT FROM THE COMPAT ROOT. It is the transport the
# NFS root is served across. If a page of its own executable had to be faulted
# in from an NFS mount, the fault would need the daemon that is blocked
# servicing it. /run is tmpfs -- local RAM -- so a copy there is immune. This is
# the same reason the DP keeps its initramfs at /oldroot.
#
# pidof, ps AND ping ARE ALL ABSENT on this root. procps is not installed --
# that is the same gap that made `sysctl` missing and sent 10-forwarding to
# /proc/sys. iputils is not installed either. So processes are found by walking
# /proc/[0-9]*/comm, and DP liveness is judged from the mailbox agent rather
# than from ICMP. Positive evidence only: `pidof rpc.nfsd` returning nothing is
# a documented false alarm on this box and the same trap applies here.
#
# ffn-cfgagent IS STOPPED FOR THE DURATION. ffn-dpsh is single-session -- one
# shared /bin/sh on the DP -- and concurrent clients wedge it. The config agent
# pushes dp.env over that same channel on a timer, so leaving it running across
# a DP reset races the boot for the mailbox. It is restarted at the end.
#
# ---------------------------------------------------------------------------
# IF THIS GOES WRONG
# ---------------------------------------------------------------------------
# A DP that boots without its mailbox agent is COMPLETELY UNREACHABLE: no
# console, no network of its own, and ffn-dpsh is the only way in. Recovery is
# the previously staged kernel:
#
#   chroot /opt/ffn-compat /bin/sh -c \
#     'FFN_DP_KERNEL=/opt/ffn/ffn-vmlinux-6.18.49-dp-pknd4 sh /opt/ffn/dpboot8.sh'
#
# NEVER delete a previously staged DP kernel. That is the whole recovery path.
set -u

C=${FFN_COMPAT:-/opt/ffn-compat}
KREL=${FFN_DP_KERNEL_REL:-/opt/ffn/ffn-vmlinux-6.18.49-dp-nfsroot}
KFALLBACK=/opt/ffn/ffn-vmlinux-6.18.49-dp-pknd4
DPSH=${DPSH:-/usr/local/bin/ffn-dpsh}
RUNDIR=/run/ffn-dp
DPNETD=$RUNDIR/ffn_dpnetd
DPNETD_SRC=$C/usr/local/bin/ffn_dpnetd
LOG=/var/log/ffn-dp-nfsboot.log

say() { echo "nfsboot: $*" | tee -a "$LOG"; }
mkdir -p "$(dirname "$LOG")" 2>/dev/null
: > "$LOG" 2>/dev/null || LOG=/dev/null

# Find pids by name without pidof/ps. Prints one pid per line, nothing if none.
pids_of() {
	for p in /proc/[0-9]*; do
		[ -r "$p/comm" ] || continue
		[ "$(cat "$p/comm" 2>/dev/null)" = "$1" ] && echo "${p#/proc/}"
	done
	return 0
}

running() { [ -n "$(pids_of "$1")" ]; }

# ---------------------------------------------------------------------------
say "=== 0. pre-flight ==="
[ -d "$C/proc/fs/nfsd" ] || say "   WARNING: $C/proc/fs/nfsd absent -- is the NFS server up?"
THREADS=$(cat "$C/proc/fs/nfsd/threads" 2>/dev/null || echo 0)
say "   nfsd threads: ${THREADS:-0}"
if [ "${THREADS:-0}" -lt 1 ]; then
	say "   NFS SERVER IS NOT RUNNING. The DP would mount nothing and fall back"
	say "   to the initramfs. Start it first with:"
	say "     chroot $C /bin/sh /usr/local/bin/ffn-cp-nfsd.sh"
	say "   That script's insmod branch is a no-op here: this CP kernel has NFSD"
	say "   built in (/proc/filesystems lists nfsd), so only the daemons start."
	exit 2
fi

[ -e "$C$KREL" ] || { say "   kernel missing: $C$KREL"; exit 1; }
say "   kernel: $KREL ($(stat -c %s "$C$KREL" 2>/dev/null) bytes)"

# The export the DP's initramfs will ask for is hardcoded in that initramfs as
# 127.1.2.1:/opt/dproot. Confirm it exists before resetting anything.
[ -d "$C/opt/dproot/bin" ] || { say "   $C/opt/dproot has no bin/ -- refusing"; exit 2; }
say "   dproot: $(ls "$C/opt/dproot" | tr '\n' ' ')"

# ---------------------------------------------------------------------------
say "=== 1. quiesce the other users of the mailbox ==="
CFGAGENT_WAS=no
if command -v systemctl >/dev/null 2>&1 && \
   systemctl is-active --quiet ffn-cfgagent 2>/dev/null; then
	systemctl stop ffn-cfgagent && CFGAGENT_WAS=yes
	say "   stopped ffn-cfgagent (single-session ffn-dpsh; restarted at the end)"
else
	say "   ffn-cfgagent not active"
fi

# ---------------------------------------------------------------------------
say "=== 2. stop the CP end of dpnet before the DP reset ==="
# By pid. `pkill -f <pattern>` matches the caller's own argv and has killed this
# session twice already.
for p in $(pids_of ffn_dpnetd); do
	kill "$p" 2>/dev/null && say "   killed pid $p"
done
sleep 2
for p in $(pids_of ffn_dpnetd); do
	kill -9 "$p" 2>/dev/null && say "   SIGKILLed pid $p"
done
sleep 1
if running ffn_dpnetd; then
	say "   still running -- refusing to reset the DP under it"
	[ "$CFGAGENT_WAS" = yes ] && systemctl start ffn-cfgagent
	exit 3
fi
say "   stopped"

# ---------------------------------------------------------------------------
say "=== 3. stage the transport binary on tmpfs ==="
mkdir -p "$RUNDIR"
if [ ! -x "$DPNETD" ] || [ "$DPNETD_SRC" -nt "$DPNETD" ]; then
	cp "$DPNETD_SRC" "$DPNETD" && chmod 755 "$DPNETD" \
		&& say "   copied $DPNETD_SRC -> $DPNETD"
else
	say "   $DPNETD already staged ($(stat -c %s "$DPNETD") bytes)"
fi

# ---------------------------------------------------------------------------
say "=== 4. boot the DP on the NFS-root kernel ==="
# Foreground, with a hard cap. dpboot8 already waits for the mailbox agent
# itself -- that is the precondition for starting dpnet -- so there is nothing
# to poll for in parallel, and running it in the foreground keeps its output.
FFN_DP_KERNEL="$KREL" timeout 300 chroot "$C" /bin/sh /opt/ffn/dpboot8.sh \
	>/tmp/dpboot8-debian.out 2>&1
RC=$?
sed 's/^/     /' /tmp/dpboot8-debian.out | tail -24 | tee -a "$LOG"
say "   dpboot8 rc=$RC"

# ---------------------------------------------------------------------------
say "=== 5. is the mailbox agent up? ==="
agent=0
i=0
while [ "$i" -lt 24 ]; do
	S=$("$DPSH" --status 2>&1)
	case "$S" in
	*"agent v2"*) agent=1; say "   agent up after ~$((i * 5))s: $(echo "$S" | head -1)"; break ;;
	esac
	i=$((i + 1))
	sleep 5
done
if [ "$agent" != 1 ]; then
	say "   NO AGENT after 120s -- the DP is unreachable."
	say "   RECOVER WITH:"
	say "     chroot $C /bin/sh -c 'FFN_DP_KERNEL=$KFALLBACK sh /opt/ffn/dpboot8.sh'"
	[ "$CFGAGENT_WAS" = yes ] && systemctl start ffn-cfgagent
	exit 4
fi

# ---------------------------------------------------------------------------
say "=== 6. start the CP end of dpnet ==="
# The DP's nfsroot flow is already waiting for this; its window is 60s and it
# restarts its own end if we are late, so being prompt matters but being late
# is survivable.
setsid "$DPNETD" --role cp -v >/tmp/dpnetd-cp-debian.log 2>&1 </dev/null &
sleep 6
if running ffn_dpnetd; then
	say "   up (pid $(pids_of ffn_dpnetd | tr '\n' ' '))"
	ip -o addr show ffndp0 2>/dev/null | sed 's/^/     /' | tee -a "$LOG"
else
	say "   FAILED to start:"
	tail -6 /tmp/dpnetd-cp-debian.log 2>/dev/null | sed 's/^/     /' | tee -a "$LOG"
fi

# ---------------------------------------------------------------------------
say "=== 7. did the DP mount and switch? ==="
# /proc/mounts is namespace-wide, so it shows the NFS root regardless of which
# root the querying process has -- and ffn-dpsh deliberately stays on the
# initramfs, so this is the right way to ask. readlink /proc/1/root is the
# direct answer.
sw=0
i=0
while [ "$i" -lt 20 ]; do
	OUT=$("$DPSH" -c 'grep nfs /proc/mounts; echo ---; readlink /proc/1/root' -t 60 2>&1 \
		| grep -v '@@')
	case "$OUT" in
	*"127.1.2.1:/opt/dproot"*) sw=1; break ;;
	esac
	i=$((i + 1))
	sleep 6
done
printf '%s\n' "$OUT" | sed 's/^/     /' | tee -a "$LOG"
if [ "$sw" = 1 ]; then
	say "   DP IS ROOTED OVER NFS"
else
	say "   DP did NOT switch. It is still reachable on the initramfs, which is"
	say "   the designed failure mode -- ffn-nfsroot exits non-zero and the boot"
	say "   continues. Look at /oldroot and the dpnet link before retrying."
fi

say "=== 8. what the new root actually contains ==="
"$DPSH" -c 'ls /proc/1/root | tr "\n" " "; echo; ls /proc/1/root/oldroot 2>/dev/null | tr "\n" " "; echo' \
	-t 60 2>&1 | grep -v '@@' | sed 's/^/     /' | tee -a "$LOG"

# ---------------------------------------------------------------------------
if [ "$CFGAGENT_WAS" = yes ]; then
	systemctl start ffn-cfgagent && say "=== 9. ffn-cfgagent restarted ==="
fi
[ "$sw" = 1 ] || exit 5
exit 0
