#!/usr/bin/env python
"""ffn_dpstage -- push a file into DP DRAM from the CP, over PCIe.

Solves one bootstrap problem: the DP needs a binary (the dpnet daemon, a script,
a rootfs image) before it has any network to fetch it over. The CP writes the
bytes straight into DP DRAM through the BAR1 index-1 window, and the DP reads
them back out of /dev/mem with the same recipe ffn-dproot already uses:

    dd if=/dev/mem of=FILE bs=1M skip=<addr/1MB> count=<MB>   # then truncate

WHY THE BYTE REVERSAL. The CP reaches this window through a PCIe path that
reverses the bytes within every aligned 64-bit word. The DP reads its own DRAM
natively. So to make the DP see byte b at address A, the CP must place b at
(A & ~7) | (7 - (A & 7)) -- i.e. write each 8-byte group reversed. Get this wrong
and the file lands byte-scrambled in a way that looks like corrupt DRAM.

Default staging address is 0x500000: inside the already-programmed index-1
window, clear of the ffn-dpsh mailbox at 0x400000 and of the dpnet rings at
0x600000, and on a MB boundary so the dd recipe above works.

Runs on the CP. Python 2 or 3.
"""
from __future__ import print_function

import argparse
import hashlib
import mmap
import os
import sys

SYSFS = "/sys/bus/pci/devices"
DEFAULT_PCI = "0003:03:00.0"

STAGE_ADDR = 0x500000
STAGE_SIZE = 0x100000

# The window covers DP phys 0x400000-0x7FFFFF at the same offset in the BAR, so
# a DP physical address in that range IS its BAR offset.
WINDOW_LO = 0x400000
WINDOW_HI = 0x800000

# ffn-dpsh's mailbox magic, used as a non-destructive canary that the window is
# programmed and the endpoint is awake. Same check ffn_dpnetd makes.
DPSH_OFF = 0x400000
DPSH_MAGIC = b"FFNDPSH2"


def swap8(data):
    """Reverse each aligned 8-byte group -- the window's 64-bit byte swap."""
    out = bytearray(len(data))
    n = len(data) - (len(data) % 8)
    for i in range(0, n, 8):
        out[i:i + 8] = data[i:i + 8][::-1]
    if n < len(data):
        # A partial tail group would land at the wrong offsets. Callers pad to a
        # multiple of 8, so reaching here means a bug, not a recoverable case.
        raise ValueError("length %d is not a multiple of 8" % len(data))
    return bytes(out)


class Window(object):
    def __init__(self, pci, need_hi):
        path = os.path.join(SYSFS, pci, "resource2")
        self.fd = os.open(path, os.O_RDWR)
        span = (need_hi + mmap.PAGESIZE - 1) & ~(mmap.PAGESIZE - 1)
        self.mm = mmap.mmap(self.fd, span, mmap.MAP_SHARED,
                            mmap.PROT_READ | mmap.PROT_WRITE)

    def close(self):
        try:
            self.mm.close()
        finally:
            os.close(self.fd)

    def read(self, addr, n):
        pad = (8 - (n % 8)) % 8
        raw = bytes(bytearray(self.mm[addr:addr + n + pad]))
        return swap8(raw)[:n]

    def write(self, addr, data):
        pad = (8 - (len(data) % 8)) % 8
        buf = data + b"\x00" * pad
        self.mm[addr:addr + len(buf)] = swap8(buf)

    def canary_ok(self):
        return self.read(DPSH_OFF, 8) == DPSH_MAGIC


def main():
    ap = argparse.ArgumentParser(
        description="stage a file into DP DRAM over PCIe (run on the CP)")
    ap.add_argument("file", help="local file to stage")
    ap.add_argument("--addr", default=hex(STAGE_ADDR),
                    help="DP physical address (default %s)" % hex(STAGE_ADDR))
    ap.add_argument("--pci", default=DEFAULT_PCI)
    ap.add_argument("--max", default=hex(STAGE_SIZE),
                    help="refuse writes larger than this (default %s)"
                         % hex(STAGE_SIZE))
    ap.add_argument("--verify-only", action="store_true",
                    help="do not write; just read back and compare")
    ap.add_argument("--force", action="store_true",
                    help="allow an address outside the staging area")
    args = ap.parse_args()

    addr = int(args.addr, 0)
    limit = int(args.max, 0)

    with open(args.file, "rb") as f:
        data = f.read()
    want = hashlib.sha256(data).hexdigest()

    if len(data) > limit:
        sys.exit("refusing: %s is %d bytes, limit is %d (raise --max if the "
                 "target area really is that big)" % (args.file, len(data),
                                                      limit))
    if not (WINDOW_LO <= addr and addr + len(data) <= WINDOW_HI):
        sys.exit("refusing: 0x%x..0x%x is outside the index-1 window "
                 "0x%x..0x%x" % (addr, addr + len(data), WINDOW_LO, WINDOW_HI))
    if not args.force and not (STAGE_ADDR <= addr and
                               addr + len(data) <= STAGE_ADDR + STAGE_SIZE):
        sys.exit("refusing: 0x%x..0x%x leaves the staging area 0x%x..0x%x -- "
                 "that would overwrite the ffn-dpsh mailbox or the dpnet rings. "
                 "Pass --force only if you have checked what is there."
                 % (addr, addr + len(data), STAGE_ADDR,
                    STAGE_ADDR + STAGE_SIZE))

    w = Window(args.pci, addr + len(data) + 8)
    try:
        if not w.canary_ok():
            sys.exit("BAR1 window canary failed: the ffn-dpsh magic is not "
                     "readable at BAR offset 0x%x.\n"
                     "  all-ones means the sysfs `enable` trap; anything else "
                     "means index 1 is not pointed at DP phys 0x400000.\n"
                     "  Run `ffn-dpsh --status` first." % DPSH_OFF)

        if not args.verify_only:
            w.write(addr, data)

        got = w.read(addr, len(data))
        have = hashlib.sha256(got).hexdigest()
    finally:
        w.close()

    mb = addr // (1024 * 1024)
    blocks = (len(data) + 1048575) // 1048576
    print("staged  : %s" % args.file)
    print("bytes   : %d" % len(data))
    print("address : 0x%x (%d MB)" % (addr, mb))
    print("sha256  : %s" % want)
    print("readback: %s  %s" % (have, "MATCH" if have == want else "MISMATCH"))
    if have != want:
        sys.exit("readback does not match -- do not use the staged copy")
    print("")
    print("on the DP:")
    print("  dd if=/dev/mem of=/tmp/%s bs=1M skip=%d count=%d"
          % (os.path.basename(args.file), mb, blocks))
    print("  truncate -s %d /tmp/%s   # or: dd bs=1 count=%d"
          % (len(data), os.path.basename(args.file), len(data)))


if __name__ == "__main__":
    main()
