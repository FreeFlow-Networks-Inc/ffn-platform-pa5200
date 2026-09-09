#!/bin/sh
# ffn-nfsroot (DP variant) -- make the DATAPLANE actually ROOT over NFS.
#
# Installed as /sbin/ffn-nfsroot in the initramfs. ffn_init runs it ONCE and
# then falls through to the boot flow that already worked, so this script must
# be safe to auto-run: idempotent, and on ANY failure it exits non-zero and
# leaves the DP booting exactly as it does without it.
#
# It must NOT hold the process open on failure. The DP's mailbox agent
# (ffn_dpagent2) comes up through the flow after this call, and that agent is
# the only way the DP is reachable at all -- an earlier draft looped here
# forever, which would have traded the control channel for a root filesystem.
#
#     MP 127.1.1.1  --pcnet-->  CP 127.1.1.2 / 127.1.2.1  --dpnet-->  DP 127.1.2.2
#
# The DP's root is /opt/dproot on the CP, which is itself
# /opt/ffn-cproot-owrt/opt/dproot on the MP -- so the DP's filesystem is
# authored on the MP's SSD and needs no staging. See octeon/NFS-LAYERING.md.
#
# ---------------------------------------------------------------------------
# WHY NOT `root=/dev/nfs nfsroot=... ip=...` ON THE KERNEL COMMAND LINE
# ---------------------------------------------------------------------------
# NFS-LAYERING.md proposes exactly that, and it CANNOT work here. The CP<->DP
# link is a TAP device created by ffn_dpnetd, a USERSPACE daemon
# (ffn_dpnetd.c: open("/dev/net/tun"), TUNSETIFF, IFF_TAP). The kernel's `ip=`
# autoconfiguration and its Root-NFS mount both run in prepare_namespace(),
# BEFORE any userspace exists -- so at that moment there is no interface to
# configure and no route to the server. CONFIG_ROOT_NFS=y and CONFIG_IP_PNP=y
# are both set in the DP kernel and are simply unreachable for this transport.
#
# The mount therefore has to happen from userspace, which means from here.
#
# ---------------------------------------------------------------------------
# WHO DOES THE RE-ROOT, AND WHY NOT pivot_root
# ---------------------------------------------------------------------------
# pivot_root(2) CANNOT be used here. The initramfs is rootfs -- /proc/mounts on
# the DP reads "rootfs / rootfs" -- and rootfs's mount has no parent, which is
# the documented EINVAL case for pivot_root. switch_root exists for exactly
# this situation and its semantics are MS_MOVE onto / followed by chroot.
#
# busybox switch_root in turn refuses unless it is PID 1, and this script is a
# child: ffn_init's run_shell() forks before exec. So the split is
#
#   here            bring up the transport, mount, validate, stage  (can fail)
#   ffn_init (pid 1)  MS_MOVE + chroot                              (cannot)
#
# and the handover is the SWITCH_FLAG file written at the end.
#
# THE TRANSPORT'S BINARY MUST NEVER BE FETCHED OVER THE TRANSPORT.
#
# ffn_dpnetd provides the link the NFS root is served across. If a page of its
# own executable ever had to be faulted in from that mount, the fault would
# need the daemon that is blocked servicing it. MS_MOVE detaches the old root,
# so step 5 binds the initramfs in at /oldroot first: the already-running
# daemon is safe regardless (its mapped pages keep the inode alive) but this
# keeps it RESTARTABLE without touching NFS. It is statically linked too, so
# there is no shared library to fault either.
#
# This is the same arrangement the CP already survives on: its ffn_pcnetd runs
# with root "/" -- the NFS mount it is itself the transport for.
set -u

DPNETD=/sbin/ffn_dpnetd
CPADDR=127.1.2.1
EXPORT=/opt/dproot
NEW=/newroot
OLD=oldroot                 # relative to $NEW
WAIT=60                     # seconds to wait for the CP

say() { echo "ffn-nfsroot: $*"; }

# On failure: say why and EXIT NON-ZERO. Deliberately not `exec /bin/sh`.
#
# ffn_init runs this once and then falls through to the boot flow that already
# works -- the one the DP's mailbox agent comes up through. Holding the process
# open with a shell here would stall that, and the agent is the only way the DP
# is reachable. Exiting hands control straight back, so a failed mount costs
# nothing: the DP boots exactly as it does without this script.
fail() { say "$* -- staying on the initramfs"; exit 1; }

# --- 0. are we already the NFS root? --------------------------------------
# A copy of this script is installed in the DP root too, so after a successful
# switch ffn_init re-execs it from there. Recognise that and just give a
# shell instead of trying to switch a second time.
#
# Test the MOUNT, not the presence of some file: any file-based marker breaks
# the moment the DP root gets populated with more of the initramfs's binaries,
# which is exactly what step 4 below starts doing.
if mount 2>/dev/null | grep -q '^[^ ]* on / type nfs'; then
	say "/ is already an NFS mount -- nothing to do"
	exec /bin/sh
fi

# --- 1. which plane is this? ----------------------------------------------
# The CP and DP kernels are built from the SAME CONFIG_INITRAMFS_SOURCE, so
# this file ships on both and must not hijack the CP's boot. The CP is a
# CN73XX with 8 cores; the DP is a CN7885 with 40. Core count is the
# discriminator, with the SoC string as a second opinion.
CORES=$(grep -c '^processor' /proc/cpuinfo 2>/dev/null || echo 0)
if [ "$CORES" -lt 16 ]; then
	say "$CORES cores -- this is the CP, not the DP; leaving its boot alone"
	exec /bin/sh
fi
say "$CORES cores -- dataplane"

# --- 2. the transport ------------------------------------------------------
# The daemon configures its own TAP: ffndp0 at 127.1.2.2/24, MTU, and
# route_localnet (needed because this link lives in non-routable 127/8).
# --wait makes it block for the CP's region magic instead of dying if the CP
# end is not up yet, which is the normal case on a cold DP boot.
if [ ! -x "$DPNETD" ]; then
	fail "$DPNETD is missing from the initramfs"
fi
if ! pidof ffn_dpnetd >/dev/null 2>&1; then
	say "starting ffn_dpnetd --role dp"
	setsid "$DPNETD" --role dp -v --wait 30 >/tmp/dpnetd-dp.log 2>&1 </dev/null &
fi

say "waiting up to ${WAIT}s for the CP at $CPADDR ..."
ok=0
i=0
while [ "$i" -lt "$WAIT" ]; do
	if ping -c 1 -W 1 "$CPADDR" >/dev/null 2>&1; then ok=1; break; fi
	# Restart the daemon if it gave up. Its --wait is 30s but ours is
	# longer, and the CP end cannot come up until the session agent has
	# published the mailbox magic -- so on a cold boot the daemon can
	# legitimately time out once before the CP is ready. Without this the
	# ping loop spins for the full window against a dead local end and
	# then reports "CP never answered", blaming the wrong side.
	if ! pidof ffn_dpnetd >/dev/null 2>&1; then
		say "  dpnetd exited; restarting it (${i}s elapsed)"
		setsid "$DPNETD" --role dp -v --wait 30 \
			>>/tmp/dpnetd-dp.log 2>&1 </dev/null &
	fi
	i=$((i + 1))
	sleep 1
done
[ "$ok" = 1 ] || fail "CP never answered on $CPADDR"
say "CP reachable"

# --- 3. mount the root -----------------------------------------------------
# nolock deliberately, on every hop: the CP's own root uses it, and this is an
# NFS RE-EXPORT (the CP re-exports a directory inside its own NFS root), so
# lockd across two hops is not something to inherit by accident.
mkdir -p "$NEW"
if ! mount -t nfs -o nolock,vers=3 "$CPADDR:$EXPORT" "$NEW" 2>/tmp/nfsmount.err; then
	say "mount failed:"
	sed 's/^/  /' /tmp/nfsmount.err
	fail "cannot mount $CPADDR:$EXPORT"
fi
# Prove it is the real root and not an empty mountpoint.
if [ ! -x "$NEW/bin/busybox" ]; then
	umount "$NEW" 2>/dev/null
	fail "$CPADDR:$EXPORT mounted but has no /bin/busybox"
fi
say "mounted $CPADDR:$EXPORT"

# --- 4. carry the transport into the new root -----------------------------
# So the daemon is restartable after the switch WITHOUT reading it back over
# NFS. Copy only if it differs, because the target is the MP's disk.
if [ ! -e "$NEW/sbin/ffn_dpnetd" ]; then
	cp "$DPNETD" "$NEW/sbin/ffn_dpnetd" 2>/dev/null \
		&& say "copied ffn_dpnetd into the new root"
fi

# --- 5. prepare the new root for the switch -------------------------------
# /proc, /sys and /dev must be visible inside the new root before the switch,
# or the daemon's route_localnet write and anything reading /proc/mounts
# breaks the moment / changes.
for d in proc sys dev; do
	mkdir -p "$NEW/$d"
	mountpoint -q "$NEW/$d" || mount --bind "/$d" "$NEW/$d" 2>/dev/null
done

# Bind the CURRENT root (the initramfs) in under the new root, so it is still
# reachable at /$OLD after the switch. This is what keeps the transport
# restartable: ffn_dpnetd, ffn_dpagent and ffn_cpdpd live there, and they are
# the only copies reachable WITHOUT going over the link they themselves
# provide. MS_MOVE detaches the old root mount, so without this bind the
# initramfs becomes unreachable -- fine for the already-running daemon, whose
# mapped pages keep its inode alive, but it could never be restarted.
mkdir -p "$NEW/$OLD"
mountpoint -q "$NEW/$OLD" || mount --bind / "$NEW/$OLD" 2>/dev/null

# --- 6. hand the switch to PID 1 ------------------------------------------
# WHY WE DO NOT DO IT OURSELVES.
#
# The switch has to be MS_MOVE of the new root onto / followed by chroot --
# switch_root's semantics. pivot_root CANNOT be used: this root is rootfs
# (/proc/mounts says "rootfs / rootfs"), whose mount has no parent, and
# pivot_root(2) fails with EINVAL in exactly that case. That is precisely why
# switch_root exists.
#
# And busybox switch_root refuses to run unless it is PID 1, which this script
# is not: ffn_init's run_shell() forks before exec (NR_fork, ffn_init.c). So
# the last step belongs to PID 1, and ffn_init does it when it sees this flag.
# Everything fragile -- transport, mount, validation -- has already happened
# here, where a failure is recoverable into a console.
echo "$NEW" > /ffn-switch-root
say "root staged at $NEW; handing the switch to PID 1"
exit 0
