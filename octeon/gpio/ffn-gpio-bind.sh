#!/bin/sh
# Runs ON the CP. Binds the OCTEON GPIO driver to the GPIO block.
#
# WHY THIS EXISTS AT ALL. The driver is built in (CONFIG_GPIO_OCTEON=y) and the
# device tree describes the block, but nothing binds: the DT says
# `cavium,octeon-7890-gpio` and upstream's match table lists only
# `cavium,octeon-3860-gpio`. There is no error anywhere -- a platform device
# nobody claims is not an error condition -- so it presents as a simple absence:
# driver registered, device present, no /dev/gpiochip0.
#
#   /sys/bus/platform/drivers/octeon_gpio          <- driver, no bound devices
#   /sys/bus/platform/devices/1070000000800.gpio-controller   <- device, no driver
#
# THE PROPER FIX is octeon/patches/upstream-6.18/tranche3-modules/
# 0006-gpio-octeon-octeon3-support.patch, which adds the compatible and the
# OCTEON III register layout. But GPIO_OCTEON is built in, so that needs a
# kernel rebuild and a CP reboot -- and rebooting the CP takes the switch daemon
# down with it, and the dataplane and its PCIe transport after that. This script
# is the no-reboot path for the kernel that is already running.
#
# driver_override is the kernel's own supported mechanism, not a trick:
# platform_match() consults it FIRST and short-circuits, so the of_match_table
# is bypassed. Manual `bind` alone will NOT do -- bind_store() still calls
# driver_match_device(), and says so in a comment.
#
# SAFE, and specifically checked: octeon_gpio_probe() allocates, ioremaps, fills
# in the gpio_chip and calls devm_gpiochip_add_data. It writes NO registers.
# Binding does not disturb a single pin.
#
# Idempotent. Reverse with: echo 1070000000800.gpio-controller > .../unbind
set -u

DEV=1070000000800.gpio-controller
DRV=/sys/bus/platform/drivers/octeon_gpio
say() { echo "  gpio: $*"; }

[ -d "$DRV" ] || { say "no octeon_gpio driver -- CONFIG_GPIO_OCTEON not built?"; exit 2; }
[ -e "/sys/bus/platform/devices/$DEV" ] || { say "no $DEV in the device tree"; exit 2; }

if [ -e "/sys/bus/platform/devices/$DEV/driver" ]; then
  say "already bound to $(basename "$(readlink -f "/sys/bus/platform/devices/$DEV/driver")")"
else
  echo octeon_gpio > "/sys/bus/platform/devices/$DEV/driver_override" \
    || { say "driver_override write FAILED"; exit 3; }
  echo "$DEV" > "$DRV/bind" || { say "bind FAILED"; exit 3; }
  say "bound"
fi

# gpiolib names the chardev by allocation order, so do not assume gpiochip0.
# The node is a direct child of the platform device -- there is no gpio/
# subdirectory, and no ngpio file either: that belongs to GPIO_SYSFS, which is
# not built here. Use `ffn_gpio.py --info` for the line count.
CHIP=$(ls -d /sys/bus/platform/devices/$DEV/gpiochip* 2>/dev/null | head -1)
[ -n "$CHIP" ] && say "gpiolib chip: $(basename "$CHIP")"
ls /dev/gpiochip* >/dev/null 2>&1 && say "chardev: $(ls /dev/gpiochip* | tr '\n' ' ')" \
  || say "NO /dev/gpiochip* -- is CONFIG_GPIO_CDEV set?"

# READS ARE CORRECT ON THIS KERNEL, DIRECTION CHANGES ARE NOT. The driver reads
# RX_DAT at base+0x80, which is right for OCTEON III. Its direction path uses
# the CN3xxx layout for the config registers, so on this chip those writes miss
# GPIO_BIT_CFG entirely and land on an undefined offset -- harmless in that no
# pin changes, but it means gpiolib's idea of a line's direction is decoupled
# from the hardware until the patch above is in the running kernel.
say "reads OK; direction changes are ineffective until patch 0006 is running"
