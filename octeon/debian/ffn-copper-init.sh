#!/bin/sh
set -eu
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
grep -q '^ffn_mdioctl ' /proc/modules || /usr/bin/busybox insmod /usr/local/lib/ffn/modules/ffn_mdioctl.ko
gate=/sys/module/ffn_mdioctl/parameters/allow_writes
trap 'echo 0 > "$gate"' EXIT
echo 1 > "$gate"
for phy in 16 17 18 19; do
    python3 /usr/local/sbin/ffn_84848_boot.py \
        /opt/ffn-compat/opt/ffn/owner-firmware/bcm84844.bin --phy "$phy" --enable
done
