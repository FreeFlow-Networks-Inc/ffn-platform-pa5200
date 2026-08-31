#!/usr/bin/env python
"""ffn-dpmem -- read DP DRAM from the CP safely, and de-swap it.

Replaces oct-remote-dump, which leaks preempt_count and has twice ended in an NMI
watchdog + "Chip soft reset" on the CP. This only mmaps the endpoint's windowed
BAR, which has never destabilised anything.

Addressing: sysfs resource2 is OCTEON BAR1, a 64 MB aperture split into 4 MB
windows. Window N is at BAR offset N*4MB and points wherever PEM0_BAR1_INDEXN
says. With PEM0_BAR1_INDEX0 = 0x1 (ADDR_IDX 0, ENA), BAR offset 0..4MB reads DRAM
0..4MB directly -- that range holds the bootloader mailbox at 0x6C000.

Byte order: content written through this path lands byte-reversed within each
64-bit word relative to a big-endian read on the CP (the vendor tool reports
"Remote host is LE" while "memory access is BE"). Proof: the queued command
"bootoctl..." reads back as "ltcotoob". So --swap un-reverses each 8-byte group,
which is what makes strings legible.
"""
from __future__ import print_function

import argparse
import mmap
import os
import struct
import sys

DP_PCI = "0003:03:00.0"
SYSFS = "/sys/bus/pci/devices"
WINDOW_BAR = 2
WINDOW_SIZE = 4 << 20          # one BAR1 index covers 4 MB


def enable(pci):
    try:
        fh = open(os.path.join(SYSFS, pci, "enable"), "w")
        try:
            fh.write("1")
        finally:
            fh.close()
    except Exception:
        pass


def read_bar(pci, offset, length):
    path = os.path.join(SYSFS, pci, "resource%d" % WINDOW_BAR)
    fd = os.open(path, os.O_RDONLY)
    try:
        span = offset + length
        mm = mmap.mmap(fd, span, mmap.MAP_SHARED, mmap.PROT_READ)
        try:
            return bytes(bytearray(mm[offset:offset + length]))
        finally:
            mm.close()
    finally:
        os.close(fd)


def unswap8(data):
    """Reverse each aligned 8-byte group."""
    out = bytearray(len(data))
    src = bytearray(data)
    n = (len(src) // 8) * 8
    for i in range(0, n, 8):
        out[i:i + 8] = src[i:i + 8][::-1]
    if n < len(src):
        out[n:] = src[n:]
    return bytes(out)


def printable_runs(data, minlen=8):
    runs = []
    cur = bytearray()
    start = 0
    for i, b in enumerate(bytearray(data)):
        if 32 <= b < 127 or b in (9, 10, 13):
            if not cur:
                start = i
            cur.append(b)
        else:
            if len(cur) >= minlen:
                runs.append((start, cur.decode("ascii", "replace")))
            cur = bytearray()
    if len(cur) >= minlen:
        runs.append((start, cur.decode("ascii", "replace")))
    return runs


def main():
    ap = argparse.ArgumentParser(description="read DP DRAM from the CP")
    ap.add_argument("--pci", default=DP_PCI)
    ap.add_argument("--offset", default="0x0",
                    help="BAR offset (with index0 on DRAM 0 this is the DRAM addr)")
    ap.add_argument("--length", default="0x1000")
    ap.add_argument("--swap", action="store_true",
                    help="reverse each 8-byte group (needed for legible text)")
    ap.add_argument("--strings", action="store_true", help="print printable runs")
    ap.add_argument("--grep", default=None, help="only runs containing this")
    ap.add_argument("--minlen", type=int, default=8)
    args = ap.parse_args()

    off = int(args.offset, 0)
    length = int(args.length, 0)
    enable(args.pci)
    try:
        data = read_bar(args.pci, off, length)
    except Exception as exc:
        print("read failed: %s" % exc)
        return 2
    if all(b == 0xFF for b in bytearray(data[:16])):
        print("all-ones: window not answering (MSE clear, or index not programmed)")
        return 2
    if args.swap:
        data = unswap8(data)

    if args.strings or args.grep:
        runs = printable_runs(data, args.minlen)
        if args.grep:
            needle = args.grep.lower()
            runs = [r for r in runs if needle in r[1].lower()]
        if not runs:
            print("(no matching printable runs in 0x%x..0x%x)" % (off, off + length))
        for pos, text in runs:
            print("  +0x%06x  %s" % (off + pos, text.strip()))
        return 0

    for i in range(0, min(len(data), length), 16):
        chunk = bytearray(data[i:i + 16])
        print("  0x%08x  %s" % (off + i, " ".join("%02x" % b for b in chunk)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
