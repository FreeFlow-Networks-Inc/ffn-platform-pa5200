#!/bin/sh
# Run on CP. Serialize boot and mailbox use; keep the proven NFS-root policy.
set -eu
K=${1:?usage: test-dp-kernel.sh ABSOLUTE_KERNEL}
test -s "$K"
sha256sum -c "$K.sha256"
for p in $(pidof ffn_dpnetd 2>/dev/null); do kill "$p"; done
sleep 2
if pidof ffn_dpnetd >/dev/null 2>&1; then echo 'dpnet still active; refusing reset'; exit 1; fi
# dpboot8 itself waits for the agent. A second mailbox poller while vendor
# boot tools are still manipulating the endpoint is unsafe and unnecessary.
FFN_DP_KERNEL="$K" sh /opt/ffn/dpboot8.sh
setsid "${DPNETD:-/usr/local/bin/ffn_dpnetd}" --role cp -v >/tmp/dpnetd-cp.log 2>&1 </dev/null &
i=0
until ping -c1 -W1 127.1.2.2 >/dev/null 2>&1; do
    i=$((i+1)); test "$i" -lt 45 || { tail /tmp/dpnetd-cp.log; exit 1; }
    sleep 2
done
sleep 3
ffn-dpsh -t 20 -c 'uname -r; cat /proc/1/comm; cat /proc/1/root/etc/os-release; grep " / " /proc/mounts; zcat /proc/config.gz | grep -E "^CONFIG_(CGROUPS|NAMESPACES|NET_NS|USER_NS)="'
