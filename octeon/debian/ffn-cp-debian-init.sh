#!/bin/sh
# Invoked by PID 1 after its RAM-backed pcnet supervisor has started.
BB=/bin/busybox
NEW=/newroot
SERVER=127.1.1.1
EXPORT=/opt/ffn-cproot-debian-20260909
say() { echo "FFN-DEBIAN: $*"; }
fallback() {
    say "$*; keeping the initramfs and console"
    while :; do $BB sh -i </dev/console >/dev/console 2>&1; $BB sleep 2; done
}
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
$BB chroot "$NEW" /usr/lib/systemd/systemd --version || fallback 'systemd runtime failed'
$BB chroot "$NEW" /usr/sbin/sshd -t || fallback 'SSH configuration failed'
for d in proc sys dev; do
    $BB mkdir -p "$NEW/$d"
    $BB mount -o bind "/$d" "$NEW/$d" || fallback "bind $d failed"
done
# Transitional management tools and NFS-server userspace retain their musl
# runtime in a separate chroot. Debian remains the actual boot root/PID 1.
COMPAT=$NEW/opt/ffn-compat
$BB mkdir -p "$COMPAT"
/sbin/ffn_nfsmount "$SERVER:/opt/ffn-cproot-owrt" "$COMPAT" \
    nolock,vers=3,addr=$SERVER,proto=tcp,mountproto=tcp,hard || fallback 'compatibility root mount failed'
for d in proc sys dev; do
    $BB mount -o bind "/$d" "$COMPAT/$d" || fallback "compatibility bind $d failed"
done
$BB mkdir -p "$NEW/run" "$NEW/oldroot"
$BB mount -t tmpfs -o mode=755 tmpfs "$NEW/run" || fallback 'run tmpfs failed'
$BB mount -o bind / "$NEW/oldroot" || fallback 'preserve initramfs failed'
say 'handing PID 1 to Debian systemd; transport remains in RAM'
exec /sbin/ffn-systemd-handoff "$NEW"
