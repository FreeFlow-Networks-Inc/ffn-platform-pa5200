#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""ffn_i2cscan.py -- find what is on the control plane's I2C buses.

Runs ON the control plane. The OCTEON TWSI controllers are described in the
device tree and the in-kernel driver binds them, so i2c-0 and i2c-1 exist as
adapters -- but the kernel was built without CONFIG_I2C_CHARDEV, so there was no
way to reach them from userspace. Loading i2c-dev.ko (built against this exact
tree, vermagic 6.18.49-dirty) creates /dev/i2c-N without touching the running
kernel.

PROBES BY READING, NEVER BY WRITING. i2cdetect's default mode uses SMBus "quick
write" for part of the range, which is a zero-length WRITE to an address nobody
has identified yet. On a board whose I2C controls PHY resets, SFP transmitters
and power, that is a bad way to discover what is there. A one-byte read tells
you something answered without asking it to do anything.

The cost is that write-only devices do not show up. That is the right trade
here: a missed device costs another look, an unintended write costs a hardware
state nobody meant to change.

Reserved addresses are skipped -- 0x00-0x07 and 0x78-0x7f are not device
addresses and probing them is meaningless.
"""
import argparse
import fcntl
import os
import sys

I2C_SLAVE_FORCE = 0x0706   # bind even if a kernel driver claims the address
I2C_SLAVE = 0x0703


def probe(bus, addr, force=False):
    """One-byte read. True if something acknowledged."""
    path = "/dev/i2c-%d" % bus
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError as exc:
        raise SystemExit("cannot open %s: %s" % (path, exc))
    try:
        try:
            fcntl.ioctl(fd, I2C_SLAVE_FORCE if force else I2C_SLAVE, addr)
        except OSError:
            return None          # address is claimed by a driver
        try:
            os.read(fd, 1)
            return True
        except OSError:
            return False
    finally:
        os.close(fd)


# Addresses worth calling out by name when found. From the vendor's own board
# code and from what these parts conventionally are.
KNOWN = {
    0x22: "PCA9555-class expander -- SFP module PRESENCE (vendor)",
    0x23: "PCA9555-class expander -- SFP TX_DISABLE (vendor)",
    0x50: "SFP/QSFP module EEPROM (A0)",
    0x51: "SFP module diagnostics (A2)",
    0x57: "board EEPROM (already claimed by a kernel driver here)",
}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--buses", default="0,1")
    ap.add_argument("--force", action="store_true",
                    help="probe addresses a kernel driver has claimed")
    args = ap.parse_args()

    for bus in [int(b) for b in args.buses.split(",") if b.strip()]:
        if not os.path.exists("/dev/i2c-%d" % bus):
            print("bus %d: no /dev/i2c-%d -- is i2c-dev loaded?" % (bus, bus))
            continue
        found, claimed = [], []
        for addr in range(0x08, 0x78):
            r = probe(bus, addr, args.force)
            if r is None:
                claimed.append(addr)
            elif r:
                found.append(addr)
        print("bus %d:" % bus)
        for a in found:
            print("   0x%02x  ACK        %s" % (a, KNOWN.get(a, "")))
        for a in claimed:
            print("   0x%02x  (in use by a kernel driver) %s"
                  % (a, KNOWN.get(a, "")))
        if not found and not claimed:
            print("   nothing answered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
