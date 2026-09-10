#!/bin/sh
set -eu
if [ ! -d /sys/module/ffn_octeon_aes ]; then
    /usr/sbin/modprobe crypto_algapi
    /usr/bin/busybox insmod /usr/local/lib/ffn_octeon_aes.ko
fi
grep -q '^driver[[:space:]]*: aes-ffn-octeon$' /proc/crypto
