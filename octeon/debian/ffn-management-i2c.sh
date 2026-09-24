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
