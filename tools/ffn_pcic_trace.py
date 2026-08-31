#!/usr/bin/env python3
"""Trace the PCIC ring while the OCTEON boots -- and keep it programmed.

Why this exists: every measurement of "is the OCTEON fetching?" so far was taken
AFTER the boot, and the console shows the OCTEON tearing down its packet pools on
panic ("Performing FPA pool 0 shutdown"). The SLI is unconfigured by then, so an
instruction count of 0 measured at that point says nothing at all -- it was
measuring a corpse. The only informative window is between "pcic: module loaded"
and the panic.

So this samples the interesting registers continuously, prints every change with
a timestamp, and re-programs the ring whenever its configuration is lost (which
the OCTEON reset does). Two jobs in one loop because they need the same tight
cadence and must not race each other for the device.

  +0x20  doorbell        we add to it, one per descriptor posted
  +0x40  instr count     the OCTEON increments as it takes instructions
  +0xb0  credits         RX buffer allowance
  +0x30  input size      512 when programmed, 128 after a reset
"""
import argparse
import fcntl
import mmap
import os
import struct
import sys
import time

RES = "/sys/bus/pci/devices/0000:01:00.0/resource0"
DEV = "/dev/ffn_pcic"
FFN_PCIC_PROGRAM = (ord("F") << 8) | 3

B = 0x10000
WATCH = [("size", 0x30), ("dbell", 0x20), ("hw_idx", 0x24), ("instr", 0x40), ("pkt_cnt", 0xb0)]
WANT_SIZE = 512
STAT = "/sys/class/net/pcicp0/statistics/%s"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=int, default=340)
    ap.add_argument("--interval", type=float, default=0.1)
    a = ap.parse_args()

    fd = os.open(RES, os.O_RDONLY | os.O_SYNC)
    m = mmap.mmap(fd, B + 0x200, prot=mmap.PROT_READ)

    def regs():
        return {n: struct.unpack("<I", m[B + o:B + o + 4])[0] for n, o in WATCH}

    def nic():
        try:
            return (int(open(STAT % "tx_packets").read()),
                    int(open(STAT % "rx_packets").read()))
        except OSError:
            return (-1, -1)

    def rearm():
        try:
            f = os.open(DEV, os.O_RDWR)
        except OSError as e:
            return "open failed: %s" % e
        try:
            fcntl.ioctl(f, FFN_PCIC_PROGRAM)
            return "ok"
        except OSError as e:
            return "errno %d" % e.errno
        finally:
            os.close(f)

    print("tracing for %ds; instr moving at all is the answer" % a.watch)
    print("  %8s %6s %8s %7s %8s %9s %6s %6s" %
          ("t", "size", "dbell", "hw_idx", "instr", "pkt_cnt", "tx", "rx"))
    t0 = time.time()
    last = None
    peak_instr = 0
    while time.time() - t0 < a.watch:
        r = regs()
        tx, rx = nic()
        key = (r["size"], r["dbell"], r["hw_idx"], r["instr"], r["pkt_cnt"], tx, rx)
        if key != last:
            print("  %8.1f %6d %8d %7d %8d %9d %6d %6d" %
                  (time.time() - t0, r["size"], r["dbell"], r["hw_idx"], r["instr"],
                   r["pkt_cnt"], tx, rx))
            sys.stdout.flush()
            last = key
        peak_instr = max(peak_instr, r["instr"], r["hw_idx"])
        if r["size"] != WANT_SIZE:
            res = rearm()
            if res != "ok":
                print("  %8.1f  re-arm: %s" % (time.time() - t0, res))
                sys.stdout.flush()
        time.sleep(a.interval)

    print()
    print("peak consumption signal (instr or hw_idx): %d" % peak_instr)
    print("VERDICT: %s" % ("the OCTEON FETCHED at least once"
                           if peak_instr else "the OCTEON never fetched"))
    m.close()
    os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
