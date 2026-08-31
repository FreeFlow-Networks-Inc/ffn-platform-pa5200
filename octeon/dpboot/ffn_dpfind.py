#!/usr/bin/env python
"""ffn-dpfind -- fast pattern search across DP DRAW via all 16 BAR1 windows.

The slow path (extracting every printable run in Python) took ~30 s per 4 MB on
the CP. This instead programs PEM0_BAR1_INDEX0..15 to map DRAM 0..64 MB
CONTIGUOUSLY onto the 64 MB BAR (index N -> ADDR_IDX N -> BAR offset N*4MB), then
searches the whole aperture with bytes.find(), which runs at C speed.

Point: locate the OCTEON PCIe console. It is a cvmx bootmem named block called
"__pci_console"; finding that name in DRAM gives us the block header, and from
there the console's output buffer -- which is the only way to see what the DP's
u-boot prints (no DP UART reaches the host, and bootoctlinux fails silently).

Accesses through this window arrive byte-reversed within each aligned 64-bit
word, so an 8-byte needle must be reversed before searching. Needles shorter than
8 bytes are searched in both orders.
"""
from __future__ import print_function

import mmap
import os
import sys

PCI = "0003:03:00.0"
SYSFS = "/sys/bus/pci/devices"
BAR_LEN = 64 << 20


def rev8(needle):
    """Reverse a needle the way an aligned 8-byte group appears through the window."""
    b = bytearray(needle)
    out = bytearray()
    for i in range(0, len(b) - 7, 8):
        out += b[i:i + 8][::-1]
    return bytes(out)


def main():
    needles = sys.argv[1:] or ["__pci_console", "__tmp_load", "U-Boot"]
    path = os.path.join(SYSFS, PCI, "resource2")
    try:
        fd = os.open(path, os.O_RDONLY)
    except Exception as exc:
        print("open %s: %s" % (path, exc))
        return 2
    try:
        try:
            mm = mmap.mmap(fd, BAR_LEN, mmap.MAP_SHARED, mmap.PROT_READ)
        except Exception as exc:
            print("mmap 64MB: %s" % exc)
            return 2
        try:
            head = bytearray(mm[0:16])
            if all(b == 0xFF for b in head):
                print("window not answering (all-ones) -- MSE clear or indexes "
                      "not programmed")
                return 2
            data = mm[:]                      # one 64 MB read
            for text in needles:
                raw = text.encode()
                hits = []
                for label, pat in (("plain", raw), ("swapped", rev8(raw))):
                    if not pat:
                        continue
                    pos = data.find(pat)
                    while pos != -1 and len(hits) < 8:
                        hits.append((label, pos))
                        pos = data.find(pat, pos + 1)
                if hits:
                    for label, pos in hits:
                        print("  FOUND %-14r %-8s at BAR 0x%08x (= DRAM 0x%08x)"
                              % (text, label, pos, pos))
                else:
                    print("  not found: %r (searched DRAM 0..64MB both orders)"
                          % text)
        finally:
            mm.close()
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
