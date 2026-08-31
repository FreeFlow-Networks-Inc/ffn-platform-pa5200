#!/usr/bin/env python3
"""Watch all four ring blocks and report which one the OCTEON actually touches.

Motivation: SLI_PKT_MACX_PFX_RINFO reads 0x3 on this device. That register
describes, per MAC (PEM) and PF, how many rings the function owns and where they
start -- and the OCTEON reports itself on PEM 3, not PEM 0. So "ring 0" in the
BAR window is an assumption, not a fact: if this PF's rings start at index 3, the
block we have been programming belongs to someone else, which would explain
registers that accept writes and hold them while nothing ever fetches.

This is read-only. It samples the instruction count and doorbell of every block
and reports any block whose instruction count moves, which is the OCTEON's own
signature -- the host never writes that register.

Run it across a boot. A block that moves is the answer; all four flat means the
ring index is not the problem.
"""
import argparse
import mmap
import os
import struct
import sys
import time

RES = "/sys/bus/pci/devices/0000:01:00.0/resource0"
BLOCK, STRIDE, NRINGS = 0x10000, 0x20000, 4
DBELL, INSTR, SIZE = 0x20, 0x40, 0x30


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=int, default=340)
    ap.add_argument("--interval", type=float, default=0.1)
    a = ap.parse_args()

    fd = os.open(RES, os.O_RDONLY | os.O_SYNC)
    span = BLOCK + STRIDE * NRINGS
    m = mmap.mmap(fd, span, prot=mmap.PROT_READ)

    def rd(ring, off):
        p = BLOCK + ring * STRIDE + off
        return struct.unpack("<I", m[p:p + 4])[0]

    base = {r: rd(r, INSTR) for r in range(NRINGS)}
    peak = dict(base)
    print("baseline instruction counts: " +
          ", ".join("ring%d=%d" % (r, base[r]) for r in range(NRINGS)))
    print("  %8s %s" % ("t", "  ".join("r%d(sz/db/ins)" % r for r in range(NRINGS))))

    t0 = time.time()
    last = None
    while time.time() - t0 < a.watch:
        row = []
        for r in range(NRINGS):
            sz, db, ins = rd(r, SIZE), rd(r, DBELL), rd(r, INSTR)
            peak[r] = max(peak[r], ins)
            row.append((sz, db, ins))
        if row != last:
            print("  %8.1f %s" % (time.time() - t0,
                                  "  ".join("%d/%d/%d" % t for t in row)))
            sys.stdout.flush()
            last = row
        time.sleep(a.interval)

    print()
    moved = [r for r in range(NRINGS) if peak[r] != base[r]]
    for r in range(NRINGS):
        print("  ring%d instr: %d -> peak %d%s"
              % (r, base[r], peak[r], "   <-- MOVED" if r in moved else ""))
    if moved:
        print("VERDICT: the OCTEON fetches on ring(s) %s"
              % ", ".join(str(r) for r in moved))
    else:
        print("VERDICT: no block fetched -- the ring index is not the problem")
    m.close()
    os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
