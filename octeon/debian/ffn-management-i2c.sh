#!/bin/sh
set -eu
test "$(uname -r)" = 6.18.49-ffn-debian-cp-dirty
load() {
    test -d "/sys/module/$1" || /usr/bin/busybox insmod "$2"
}
load i2c_dev /lib/modules/6.18.49-ffn-debian-cp-dirty/kernel/drivers/i2c/i2c-dev.ko
load i2c_mux /usr/local/lib/ffn-management/i2c-mux.ko
load i2c_mux_pca954x /usr/local/lib/ffn-management/i2c-mux-pca954x.ko
test -d /sys/bus/i2c/devices/1-0073 || echo pca9548 0x73 > /sys/bus/i2c/devices/i2c-1/new_device
# Disconnect after every transaction: downstream CE sensors share NB addresses.
echo -2 > /sys/bus/i2c/devices/1-0073/idle_state
test -L /sys/bus/i2c/devices/1-0073/channel-3
