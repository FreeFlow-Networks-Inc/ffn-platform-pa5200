#!/bin/sh
# Invoked by PID 1 after its RAM-backed pcnet supervisor has started.
BB=/bin/busybox
NEW=/newroot
SERVER=${FFN_MP_TRANSPORT_ADDRESS:-127.1.1.1}
EXPORT=${FFN_CP_EXPORT:-}
say() { echo "FFN-DEBIAN: $*"; }
fallback() {
    say "$*; keeping the initramfs and console"
    while :; do $BB sh -i </dev/console >/dev/console 2>&1; $BB sleep 2; done
}
[ -n "$EXPORT" ] || fallback "MP must provision FFN_CP_EXPORT"
[ "$$" = 1 ] || { say 'must be PID 1'; exit 1; }
$BB grep -qw cgroup2 /proc/filesystems || fallback 'kernel lacks cgroup v2'
say 'waiting for the management-plane transport'
i=0
until $BB ping -c1 -W1 "$SERVER" >/dev/null 2>&1; do
    i=$((i+1)); [ "$i" -lt 300 ] || fallback 'MP unreachable'
    $BB sleep 1
done
$BB mkdir -p "$NEW"
/sbin/ffn_nfsmount "$SERVER:$EXPORT" "$NEW" \
    nolock,vers=3,addr=$SERVER,proto=tcp,mountproto=tcp,hard || fallback 'NFS mount failed'
$BB grep -Eq '^ID="?debian"?$' "$NEW/usr/lib/os-release" || fallback 'Debian root required'
[ ! -e "$NEW/opt/ffn-compat" ] || fallback 'compatibility roots are retired'
$BB chroot "$NEW" /usr/lib/systemd/systemd --version || fallback 'systemd runtime failed'
$BB chroot "$NEW" /usr/sbin/sshd -t || fallback 'SSH configuration failed'
for d in proc sys dev; do
    $BB mkdir -p "$NEW/$d"
    $BB mount -o bind "/$d" "$NEW/$d" || fallback "bind $d failed"
done
$BB mkdir -p "$NEW/run" "$NEW/oldroot"
$BB mount -t tmpfs -o mode=755 tmpfs "$NEW/run" || fallback 'run tmpfs failed'
$BB mount -o bind / "$NEW/oldroot" || fallback 'preserve initramfs failed'
say 'handing PID 1 to Debian systemd; transport remains in RAM'
exec /sbin/ffn-systemd-handoff "$NEW"
