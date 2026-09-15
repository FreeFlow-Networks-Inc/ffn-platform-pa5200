#!/bin/sh
set -eu
if [ ! -d /sys/module/ffn_octeon_aes ]; then
    /usr/sbin/modprobe crypto_algapi
    if [ -f "/lib/modules/$(uname -r)/extra/ffn_octeon_aes.ko" ]; then
        /usr/sbin/modprobe ffn_octeon_aes
    else
        /usr/bin/busybox insmod /usr/local/lib/ffn_octeon_aes.ko
    fi
fi
grep -q '^driver[[:space:]]*: aes-ffn-octeon$' /proc/crypto
