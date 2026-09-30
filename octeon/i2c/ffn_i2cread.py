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
from functools import lru_cache
import os
import sys

@lru_cache(maxsize=1)
def native():
    lib = ctypes.CDLL('/usr/local/lib/libffn-hwio.so', use_errno=True)
    lib.ffn_hwio_abi.restype = ctypes.c_uint
    if lib.ffn_hwio_abi() != 1:
        raise RuntimeError('Unsupported native hardware I/O ABI')
    lib.ffn_i2c_read.argtypes = [ctypes.c_uint] * 4 + [ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint]
    lib.ffn_i2c_read.restype = ctypes.c_int
    lib.ffn_i2c_write8.argtypes = [ctypes.c_uint] * 4
    lib.ffn_i2c_write8.restype = ctypes.c_int
    return lib


def checked(result):
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def validate(*values):
    if any(type(v) is not int or not 0 <= v <= 65535 for v in values):
        raise ValueError('Invalid I2C control parameter')


def read_bare(bus, addr, count):
    return read_regs(bus, addr, 0, count, 0)


def read_regs(bus, addr, offset, count, offset_bytes=1):
    """Request one atomic native I2C transaction; no direct Python device I/O."""
    validate(bus, addr, offset, count, offset_bytes)
    if not 1 <= count <= 4096 or offset_bytes not in (0, 1, 2):
        raise ValueError('Invalid I2C transaction size')
    data = (ctypes.c_uint8 * count)()
    checked(native().ffn_i2c_read(bus, addr, offset, offset_bytes, data, count))
    return bytes(data)


def write_reg(bus, addr, offset, value):
    validate(bus, addr, offset, value)
    checked(native().ffn_i2c_write8(bus, addr, offset, value))


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
