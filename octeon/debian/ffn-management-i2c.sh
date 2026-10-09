#!/bin/sh
set -eu
# Modules come from the running kernel's Debian module tree. An upgraded CP
# must not depend on loose modules built for an earlier commissioning kernel.
modprobe i2c-dev
modprobe i2c-mux-pca954x
test -d /sys/bus/i2c/devices/1-0073 || echo pca9548 0x73 > /sys/bus/i2c/devices/i2c-1/new_device
# Disconnect after every transaction: downstream CE sensors share NB addresses.
echo -2 > /sys/bus/i2c/devices/1-0073/idle_state
for channel in 0 2 3; do
    test -L "/sys/bus/i2c/devices/1-0073/channel-$channel"
done
# SFP module diagnostics. Resolve channel symlinks at runtime: Linux adapter
# numbers depend on mux enumeration order and differ from the vendor image.
for address in 0075 0077; do
    mux="/sys/bus/i2c/devices/1-$address"
    test -d "$mux" || echo "pca9548 0x$address" > /sys/bus/i2c/devices/i2c-1/new_device
    test "$(basename "$(readlink -f "$mux/driver")")" = pca954x
    echo -2 > "$mux/idle_state"
    for channel in 0 1 2 3 4 5 6 7; do
        test -L "$mux/channel-$channel"
    done
done
