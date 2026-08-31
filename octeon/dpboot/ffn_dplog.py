#!/usr/bin/env python
"""ffn-dplog -- find a kernel log in DP DRAM and print it, in one process.

Replaces a shell pipeline that kept breaking: the CP userland has no `sed`, and
awk column numbers shift with the needle's word count. Doing the search and the
read in one Python process avoids passing addresses through the shell at all.

Assumes PEM0_BAR1_INDEX0..15 already map DRAM 0..64MB contiguously (index N ->
ADDR_IDX N), so BAR offset == DRAM address. Accesses arrive byte-reversed within
each aligned 64-bit word, so text is de-swapped per 8-byte group before printing.
"""
from __future__ import print_function

import mmap
import os
import sys

RES = "/sys/bus/pci/devices/0003:03:00.0/resource2"
BAR_LEN = 64 << 20


def rev8(b):
    out = bytearray()
    for i in range(0, len(b) - 7, 8):
        out += bytearray(b[i:i + 8])[::-1]
    return bytes(out)


def unswap(data):
    src = bytearray(data)
    out = bytearray(len(src))
    n = (len(src) // 8) * 8
    for i in range(0, n, 8):
        out[i:i + 8] = src[i:i + 8][::-1]
    if n < len(src):
        out[n:] = src[n:]
    return bytes(out)


def runs(data, minlen=6):
    res = []
    cur = bytearray()
    start = 0
    for i, ch in enumerate(bytearray(data)):
        if 32 <= ch < 127 or ch in (9, 10):
            if not cur:
                start = i
            cur.append(ch)
        else:
            if len(cur) >= minlen:
                res.append((start, cur.decode("ascii", "replace")))
            cur = bytearray()
    if len(cur) >= minlen:
        res.append((start, cur.decode("ascii", "replace")))
    return res


def main():
    needle = sys.argv[1] if len(sys.argv) > 1 else "Linux version"
    span = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0x3000
    fd = os.open(RES, os.O_RDONLY)
    try:
        mm = mmap.mmap(fd, BAR_LEN, mmap.MAP_SHARED, mmap.PROT_READ)
        try:
            if all(b == 0xFF for b in bytearray(mm[0:16])):
                print("window not answering (all-ones)")
                return 2
            blob = mm[:]
            pat = rev8(needle.encode())
            hits = []
            pos = blob.find(pat)
            while pos != -1 and len(hits) < 6:
                hits.append(pos)
                pos = blob.find(pat, pos + 1)
            if not hits:
                print("needle %r not found in DRAM 0..64MB" % needle)
                return 1
            for h in hits:
                # step back to the 8-byte group boundary so de-swapping lines up
                base = h & ~7
                print("======== log region at DRAM 0x%08x ========" % base)
                text = unswap(blob[base:base + span])
                for off, line in runs(text):
                    print("  +0x%06x  %s" % (base + off, line.strip()))
        finally:
            mm.close()
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
