#!/usr/bin/env python3
"""Keep the PCIC rings armed across an OCTEON reset.

Resetting the OCTEON clears the SLI ring registers, so whatever the driver
programmed at probe time is gone before the far side is running. This polls the
ring control register and re-arms whenever the enable disappears, for the length
of a boot.

Re-arming is idempotent -- it rewrites the same base addresses and the same
enable -- so polling is safe; the only cost of an unnecessary call is six MMIO
writes. Watching for the enable to VANISH is what makes it useful: it both proves
the reset really does clear these registers and pins down when.

  --watch N   seconds to keep watching (default 300, about one staging cycle)
"""
import argparse
import fcntl
import mmap
import os
import struct
import sys
import time

DEV = "/dev/ffn_pcic"
BDF = "0000:01:00.0"
RES = "/sys/bus/pci/devices/%s/resource0" % BDF

# _IO('F', 3): nr | magic << 8
FFN_PCIC_PROGRAM = (ord("F") << 8) | 3

RING_SIZE = 0x10030        # ring 0 input size, BAR0 relative
WANT_SIZE = 512           # what programming puts there; 128 is the reset default


def read_size():
    """Read the input-ring size register without going through the driver."""
    fd = os.open(RES, os.O_RDONLY | os.O_SYNC)
    try:
        m = mmap.mmap(fd, RING_SIZE + 8, prot=mmap.PROT_READ)
        try:
            return struct.unpack("<I", m[RING_SIZE:RING_SIZE + 4])[0]
        finally:
            m.close()
    finally:
        os.close(fd)


def rearm():
    fd = os.open(DEV, os.O_RDWR)
    try:
        fcntl.ioctl(fd, FFN_PCIC_PROGRAM)
        return True
    except OSError as e:
        print("  rearm failed: %s" % e)
        return False
    finally:
        os.close(fd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=int, default=300)
    ap.add_argument("--interval", type=float, default=0.2)
    a = ap.parse_args()

    print("watching ring 0 input size for %ds (want %d; 128 = reset default)" % (a.watch, WANT_SIZE))
    t0 = time.time()
    last = None
    lost = rearmed = 0
    while time.time() - t0 < a.watch:
        try:
            ctl = read_size()
        except OSError as e:
            print("  %6.1fs  size unreadable: %s" % (time.time() - t0, e))
            time.sleep(a.interval)
            continue
        if ctl != last:
            print("  %6.1fs  size=%d%s"
                  % (time.time() - t0, ctl,
                     "" if ctl == WANT_SIZE else "   <-- RING LOST ITS CONFIG"))
            last = ctl
        if ctl != WANT_SIZE:
            lost += 1
            if rearm():
                rearmed += 1
                last = None          # force a report of the value after re-arming
        time.sleep(a.interval)

    print("done: config seen lost %d times, re-armed %d times" % (lost, rearmed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
