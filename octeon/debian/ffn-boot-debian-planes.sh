#!/bin/bash
# MP: one serialized startup path for both Debian processors.
set -eu
FFN_CP_KNOWN=/etc/ffn-ngfw/plane_boot_known_hosts \
    bash /usr/local/libexec/ffn/test-cp-kernel.sh \
    "${FFN_CP_KERNEL:-/var/lib/ffn-ngfw/octeon/ffn-vmlinux-systemd-cp-20260909}"
/usr/local/sbin/ffn-cp "date -s @$(date +%s) >/dev/null; systemctl start ffn-dp-boot.service"
for i in $(seq 1 40); do
    if ssh -o BatchMode=yes -o ConnectTimeout=5 \
        -o UserKnownHostsFile=/etc/ffn-ngfw/plane_boot_known_hosts \
        -o 'ProxyCommand=ssh -F /etc/ffn-ngfw/ssh-cp.conf -W %h:%p ffn-cp' \
        root@127.1.2.2 "test \"\$(readlink /proc/1/exe)\" = /usr/lib/systemd/systemd && date -s @$(date +%s) >/dev/null && systemctl is-active ffn-sshd"; then
        echo 'PASS: both Debian processors reachable; MP-to-CP management ready'
        exit 0
    fi
    sleep 3
done
echo 'CP is reachable, but DP Debian SSH did not return' >&2
exit 1
