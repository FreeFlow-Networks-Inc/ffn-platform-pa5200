#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""ffn_gpio.py -- read the control plane's OCTEON GPIO lines.

Runs ON the control plane, against /dev/gpiochip0. Part of the faceplate state
this platform needs is not on I2C at all: the vendor's gryphon_read_sfp_state
has a branch that never touches a bus and instead reads GPIO_RX_DAT and shifts
out a per-port bit. This is that path, in our own code.

WHY THERE IS NO PLAIN "READ ANY LINE" MODE
------------------------------------------
There is no safe one. Reading a value through the GPIO character device requires
requesting the line first, and in the v2 uAPI the request path is:

    if (flags & GPIO_V2_LINE_FLAG_OUTPUT)
            gpiod_direction_output_nonotify(desc, val);
    else
            gpiod_direction_input_nonotify(desc);      /* unconditional */

-- gpiolib-cdev.c, linereq_set_config. The else branch has no "leave it alone"
case, so a request without the OUTPUT flag ALWAYS drives the line to input. For
gpio-octeon that means:

    octeon_gpio_dir_in() -> cvmx_write_csr(bit_cfg_reg(offset), 0)

which zeroes that line's whole configuration register, clearing tx_oe. On a line
that is currently an output driving something real -- a PHY reset, an SFP
TX_DISABLE, a power enable -- that tri-states it. On a live firewall that is a
way to turn a port off, or worse, while merely trying to look at it.

So this tool will not request a line unless it is told to. --read takes an
explicit list, and defaults to VENDOR_INPUT_BITS: the lines the vendor's own code
READS, which are therefore already inputs, making the direction write idempotent.
Anything outside that set is your assertion, not ours.

--info needs no request at all and writes nothing. Note that its direction field
is gpiolib's own bookkeeping, not measured: gpio-octeon implements no
.get_direction, so for a line nobody has requested the kernel reports its default
rather than what the pin is really doing. Do not read hardware truth from it.
"""
import argparse
import ctypes
import fcntl
import os
import struct
import sys

# The five GPIO bits gryphon_read_sfp_state shifts out on its non-I2C branch
# (libports.so, gryphon_ports.c lines 770/773/777/779/784). The vendor reads
# these, so they are inputs, so requesting them as input changes nothing.
VENDOR_INPUT_BITS = (2, 5, 7, 14, 15)

# MIPS does NOT use the generic ioctl encoding. asm-generic has dir NONE=0,
# WRITE=1, READ=2 with a 14-bit size and DIRSHIFT=30; MIPS has NONE=1, READ=2,
# WRITE=4 with a 13-bit size and DIRSHIFT=29. _IOWR happens to collide at
# 0xC0000000, but _IOR does not -- 0x40000000 here against 0x80000000 generic --
# so a hardcoded number lifted from an x86 header gets ENOTTY on this box.
_IOC_NRSHIFT, _IOC_TYPESHIFT, _IOC_SIZESHIFT, _IOC_DIRSHIFT = 0, 8, 16, 29
_IOC_READ, _IOC_WRITE = 2, 4


def _ioc(direction, typ, nr, size):
    return ((direction << _IOC_DIRSHIFT) | (size << _IOC_SIZESHIFT)
            | (typ << _IOC_TYPESHIFT) | (nr << _IOC_NRSHIFT))


_RW = _IOC_READ | _IOC_WRITE
GPIO_GET_CHIPINFO_IOCTL = _ioc(_IOC_READ, 0xB4, 0x01, 68)
GPIO_V2_GET_LINEINFO_IOCTL = _ioc(_RW, 0xB4, 0x05, 256)
GPIO_V2_GET_LINE_IOCTL = _ioc(_RW, 0xB4, 0x07, 592)
GPIO_V2_LINE_GET_VALUES_IOCTL = _ioc(_RW, 0xB4, 0x0E, 16)

FLAGS = [(1 << 0, "used"), (1 << 1, "active-low"), (1 << 2, "input"),
         (1 << 3, "output"), (1 << 4, "edge-rising"), (1 << 5, "edge-falling"),
         (1 << 6, "open-drain"), (1 << 7, "open-source"),
         (1 << 8, "pull-up"), (1 << 9, "pull-down"), (1 << 10, "bias-disabled")]
GPIO_V2_LINE_FLAG_INPUT = 1 << 2

# "=" is native byte order with standard sizes: correct on this big-endian
# target, and it keeps every pad byte explicit below rather than letting the
# compiler's alignment rules be guessed at.
E = "="


def chip_info(fd):
    buf = bytearray(68)
    fcntl.ioctl(fd, GPIO_GET_CHIPINFO_IOCTL, buf)
    name, label, lines = struct.unpack(E + "32s32sI", bytes(buf))
    return (name.split(b"\0")[0].decode(), label.split(b"\0")[0].decode(),
            lines)


def line_info(fd, offset):
    """Pure read: no line request, nothing written to the chip."""
    buf = bytearray(256)
    struct.pack_into(E + "I", buf, 64, offset)      # .offset follows two names
    fcntl.ioctl(fd, GPIO_V2_GET_LINEINFO_IOCTL, buf)
    name, consumer, off, num_attrs, flags = struct.unpack_from(
        E + "32s32sIIQ", bytes(buf), 0)
    return {"offset": off,
            "name": name.split(b"\0")[0].decode(),
            "consumer": consumer.split(b"\0")[0].decode(),
            "flags": flags,
            "num_attrs": num_attrs}


def read_lines(fd, offsets):
    """Request `offsets` as inputs and read them. WRITES bit_cfg -- see module doc."""
    req = bytearray(592)
    for i, off in enumerate(offsets):
        struct.pack_into(E + "I", req, 4 * i, off)
    struct.pack_into(E + "32s", req, 256, b"ffn-gpio")
    struct.pack_into(E + "Q", req, 288, GPIO_V2_LINE_FLAG_INPUT)  # config.flags
    struct.pack_into(E + "I", req, 560, len(offsets))              # num_lines
    fcntl.ioctl(fd, GPIO_V2_GET_LINE_IOCTL, req)
    line_fd = struct.unpack_from(E + "i", bytes(req), 588)[0]
    if line_fd < 0:
        raise OSError("kernel returned fd %d" % line_fd)
    try:
        mask = (1 << len(offsets)) - 1
        vals = bytearray(struct.pack(E + "QQ", 0, mask))
        fcntl.ioctl(line_fd, GPIO_V2_LINE_GET_VALUES_IOCTL, vals)
        bits = struct.unpack(E + "QQ", bytes(vals))[0]
        return {off: (bits >> i) & 1 for i, off in enumerate(offsets)}
    finally:
        os.close(line_fd)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chip", default="/dev/gpiochip0")
    ap.add_argument("--info", action="store_true",
                    help="list every line; writes nothing")
    ap.add_argument("--read", nargs="?", const="vendor", default=None,
                    metavar="LINES",
                    help="read values. Bare, or 'vendor', uses the lines the "
                         "vendor's code reads (%s). An explicit comma list is "
                         "your assertion that those lines are inputs -- read "
                         "the module docstring first."
                         % ",".join(str(b) for b in VENDOR_INPUT_BITS))
    args = ap.parse_args()

    if not args.info and args.read is None:
        ap.error("give --info (safe) or --read")

    try:
        fd = os.open(args.chip, os.O_RDWR)
    except OSError as exc:
        raise SystemExit("cannot open %s: %s" % (args.chip, exc))
    try:
        name, label, nlines = chip_info(fd)
        print("%s: %s (%s), %d lines" % (args.chip, name, label, nlines))

        if args.info:
            print("\n(direction below is gpiolib bookkeeping, not measured -- "
                  "gpio-octeon has no .get_direction)")
            for off in range(nlines):
                li = line_info(fd, off)
                names = [n for bit, n in FLAGS if li["flags"] & bit] or ["-"]
                print("  line %2d  flags=%#06x %-28s name=%-14r consumer=%r"
                      % (off, li["flags"], ",".join(names), li["name"],
                         li["consumer"]))

        if args.read is not None:
            if args.read in ("vendor", ""):
                offs = list(VENDOR_INPUT_BITS)
                note = "vendor-read lines"
            else:
                offs = [int(x, 0) for x in args.read.split(",") if x.strip()]
                note = "caller-specified lines"
            bad = [o for o in offs if not 0 <= o < nlines]
            if bad:
                raise SystemExit("line(s) out of range 0..%d: %s"
                                 % (nlines - 1, bad))
            print("\nreading %s: %s" % (note, ",".join(str(o) for o in offs)))
            vals = read_lines(fd, offs)
            for off in offs:
                print("  line %2d = %d" % (off, vals[off]))
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
