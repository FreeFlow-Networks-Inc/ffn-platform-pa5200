#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""ffn_i2cread.py -- read registers or EEPROM bytes from an identified I2C device.

Companion to ffn_i2cscan.py, which finds what ACKs. This reads what is actually
in a device, which is the only way to tell a real part from a stray
acknowledgement: four sensors reporting 51.5/50.0/47.0/46.5 C, or four EEPROMs
whose first bytes decode as DDR4 SPD, are self-validating in a way an ACK is not.

ON THE ONE WRITE THIS DOES. Reading a register requires transmitting the register
index first -- that is how the I2C register-read transaction is defined, and it
is not a control write. But it does put a byte on the wire, so this tool takes an
EXPLICIT address and never sweeps: point it at a part you have identified. The
scanner is the thing that is safe to aim at the unknown; this is not.

The transfer is issued as one I2C_RDWR with both messages, so it is atomic at the
adapter. Split into a separate write() and read(), another master -- or the
kernel's own EEPROM driver on the same bus -- can interleave between them and the
read returns a different register's contents.
"""
import argparse
import ctypes
import fcntl
import os
import sys

I2C_RDWR = 0x0707
I2C_M_RD = 0x0001


class Msg(ctypes.Structure):
    _fields_ = [("addr", ctypes.c_uint16), ("flags", ctypes.c_uint16),
                ("len", ctypes.c_uint16), ("buf", ctypes.POINTER(ctypes.c_uint8))]


class Ioctl(ctypes.Structure):
    _fields_ = [("msgs", ctypes.POINTER(Msg)), ("nmsgs", ctypes.c_uint32)]


def read_bare(bus, addr, count):
    """A pure read: no offset byte, nothing transmitted but the address.

    This is the only probe that puts NO data byte on the wire, and it is how you
    identify a part safely. A PCA954x multiplexer answers a bare read with its
    channel-select register, so 0x00 says "all channels off, nothing selected"
    and 0x08 says "channel 3 is live" -- enough to tell a mux from something
    else without ever writing to it.
    """
    fd = os.open("/dev/i2c-%d" % bus, os.O_RDWR)
    try:
        inb = (ctypes.c_uint8 * count)()
        msgs = (Msg * 1)(Msg(addr, I2C_M_RD, count,
                             ctypes.cast(inb, ctypes.POINTER(ctypes.c_uint8))))
        fcntl.ioctl(fd, I2C_RDWR, Ioctl(msgs, 1))
        return bytes(inb)
    finally:
        os.close(fd)


def read_regs(bus, addr, offset, count, offset_bytes=1):
    """One atomic write-offset-then-read transaction. Returns bytes."""
    fd = os.open("/dev/i2c-%d" % bus, os.O_RDWR)
    try:
        if offset_bytes == 2:
            out = (ctypes.c_uint8 * 2)((offset >> 8) & 0xff, offset & 0xff)
        else:
            out = (ctypes.c_uint8 * 1)(offset & 0xff)
        inb = (ctypes.c_uint8 * count)()
        msgs = (Msg * 2)(
            Msg(addr, 0, len(out),
                ctypes.cast(out, ctypes.POINTER(ctypes.c_uint8))),
            Msg(addr, I2C_M_RD, count,
                ctypes.cast(inb, ctypes.POINTER(ctypes.c_uint8))),
        )
        req = Ioctl(msgs, 2)
        fcntl.ioctl(fd, I2C_RDWR, req)
        return bytes(inb)
    finally:
        os.close(fd)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bus", type=int, required=True)
    ap.add_argument("--addr", required=True, help="device address, e.g. 0x57")
    ap.add_argument("--offset", default="0")
    ap.add_argument("--count", type=int, default=16)
    ap.add_argument("--offset-bytes", type=int, default=1, choices=(1, 2))
    ap.add_argument("--bare", action="store_true",
                    help="pure read, no offset byte -- the safest probe of an "
                         "unidentified part (see read_bare)")
    ap.add_argument("--ascii", action="store_true",
                    help="also show printable text")
    args = ap.parse_args()

    addr = int(args.addr, 0)
    off = 0 if args.bare else int(args.offset, 0)
    try:
        if args.bare:
            d = read_bare(args.bus, addr, args.count)
        else:
            d = read_regs(args.bus, addr, off, args.count, args.offset_bytes)
    except OSError as exc:
        print("bus %d addr 0x%02x %s: %s"
              % (args.bus, addr, "bare" if args.bare else "offset 0x%x" % off,
                 exc))
        return 1

    for i in range(0, len(d), 16):
        row = d[i:i + 16]
        line = "0x%04x  %s" % (off + i, " ".join("%02x" % b for b in row))
        if args.ascii:
            line += "  |%s|" % "".join(
                chr(b) if 32 <= b < 127 else "." for b in row)
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
