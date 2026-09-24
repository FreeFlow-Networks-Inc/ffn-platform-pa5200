#!/bin/sh
set -eu
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
test -d /sys/module/ffn_mdioctl
gate=/sys/module/ffn_mdioctl/parameters/allow_writes
trap 'echo 0 > "$gate"' EXIT
echo 1 > "$gate"
for phy in 16 17 18 19; do
    python3 /usr/local/sbin/ffn_84848_boot.py \
        /usr/share/ffn/firmware/bcm84844.bin --phy "$phy"
done
# Interface configuration owns enable/disable and speed. Firmware startup must
# not enable every copper port or renegotiate an already configured WAN.
