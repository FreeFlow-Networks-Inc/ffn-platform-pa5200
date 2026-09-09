#!/bin/sh
# Debian CP: boot its DP, then supervise the transport with systemd.
set -eu
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
C=/opt/ffn-compat
K=${FFN_DP_KERNEL:-/opt/ffn/ffn-vmlinux-systemd-dp-20260909}
systemctl stop ffn-dpnet.service
# A previous manual test may have left an unmanaged instance.
for p in $(/usr/bin/busybox pidof ffn_dpnetd 2>/dev/null); do kill "$p"; done
sleep 2
if /usr/bin/busybox pidof ffn_dpnetd >/dev/null 2>&1; then
    echo 'DP transport still running; refusing reset' >&2; exit 1
fi
mkdir -p "$C/tmp/dpfs" /run/ffn-dp
if ! mountpoint -q "$C/tmp/dpfs"; then
    /oldroot/sbin/ffn_nfsmount 127.1.1.1:/opt/dpfs "$C/tmp/dpfs" \
        ro,nolock,vers=3,addr=127.1.1.1,proto=tcp,mountproto=tcp,hard
fi
cp "$C/usr/local/bin/ffn_dpnetd" /run/ffn-dp/ffn_dpnetd
chroot "$C" sha256sum -c "$K.sha256"
chroot "$C" /usr/bin/env PATH="$PATH" FFN_DP_KERNEL="$K" /bin/bash /opt/ffn/dpboot8.sh
systemctl start ffn-dpnet.service
for i in $(seq 1 45); do
    if /usr/bin/busybox ping -c1 -W1 127.1.2.2 >/dev/null 2>&1; then exit 0; fi
    sleep 2
done
echo 'DP network did not return' >&2
exit 1
