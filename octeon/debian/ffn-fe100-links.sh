#!/bin/sh
# Initialize FE100's physical links using owner runtime already on the box.
set -eu
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export LD_LIBRARY_PATH=/opt/ffn-compat/tmp/dpfs/usr/local/lib64:/opt/ffn-compat/tmp/dpfs/usr/local/lib64/3p:/opt/ffn-compat/tmp/dpfs/usr/lib64
gate=/sys/module/ffn_mdioctl/parameters/allow_gearbox_writes
trap 'echo 0 > "$gate"' EXIT
mkdir -p /var/lib/ffn/fe100
for device in 0002:00:00.0 0002:01:00.0; do
    node=/sys/bus/pci/devices/$device/enable
    test "$(cat "$node")" != 0 || echo 1 > "$node"
done
timeout -k 5 300 python3 /usr/local/sbin/ffn_gearbox_probe.py --apply \
    --trace /var/lib/ffn/fe100/boot-gearbox.txt
timeout -k 5 120 python3 /usr/local/sbin/ffn_fe100_nif_probe.py --apply \
    --trace /var/lib/ffn/fe100/boot-nif.txt
timeout -k 5 120 python3 /usr/local/sbin/ffn_fe100_nif_probe.py --block tmi --apply \
    --trace /var/lib/ffn/fe100/boot-tmi.txt
python3 /usr/local/sbin/ffn_fe100_links_status.py --require-up
