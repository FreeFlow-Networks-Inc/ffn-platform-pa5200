#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""ffn_fe100.py -- read the FE100's control registers. Runs ON the control plane.

The FE100 is a Palo Alto ASIC (PCI feed:fe1c, class 0x020000, BAR0 = 1 MB) on
the control plane's own PCIe bus with no driver bound to it. This maps its BAR
through /dev/mem and reads registers by name from the 5951-entry map recovered
from the vendor's libpandp_cp.so.

WHY NOT devmem. The OpenWrt busybox on this control plane has no devmem applet,
and the earlier probe tool assumed one. Rather than add a package to a firewall's
control plane for one read, this uses the python3 that is already there for
ffn_bcmd.

THE BYTE SWAP IS NOT OPTIONAL. The control plane is MIPS64 big-endian and this
CSR window is not, so every 32-bit read comes back byte-reversed. That was
established by reading four registers raw and matching their documented reset
values, all four exactly:

    nif_rst_ctrl       0xFFFF0F00 -> 0x000FFFFF
    nif_p0_mac_pcs_cfg 0x90910400 -> 0x00049190
    nif_p1_mac_pcs_cfg 0x90920400 -> 0x00049290
    nif_cr_imp_chk_en  0x0F000000 -> 0x0000000F

Reading without the swap does not fail; it silently returns a different number,
which is the worst way for a diagnostic to be wrong.

READ-ONLY BY DEFAULT. Writing to this chip is how it gets configured, and it is
also how it gets wedged, so writes need --allow-write and take a register name
rather than a raw address.
"""
import argparse
import json
import mmap
import os
import re
import sys

PCI_DEV = "0002:01:00.0"
SYSFS = "/sys/bus/pci/devices/" + PCI_DEV


def bar0_base_and_size():
    """BAR0's CPU physical address and length, from sysfs rather than a constant.

    Hard-coding the base is how a tool starts lying after a rescan moves it.
    """
    with open(os.path.join(SYSFS, "resource")) as fh:
        start, end, _flags = fh.readline().split()
    start, end = int(start, 16), int(end, 16)
    return start, end - start + 1


def memory_decode_on():
    """True if the device's COMMAND register has memory space enabled.

    Checked because reads on a device with decode off do not error -- they
    return 0xffffffff, which looks exactly like a register that reads all-ones.
    """
    with open(os.path.join(SYSFS, "config"), "rb") as fh:
        fh.seek(4)
        cmd = int.from_bytes(fh.read(2), "little")
    return bool(cmd & 0x2)


def bswap32(v):
    return ((v & 0x000000FF) << 24 | (v & 0x0000FF00) << 8 |
            (v & 0x00FF0000) >> 8 | (v & 0xFF000000) >> 24)


class Fe100:
    def __init__(self):
        self.base, self.size = bar0_base_and_size()
        self.fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        self.map = mmap.mmap(self.fd, self.size, mmap.MAP_SHARED,
                             mmap.PROT_READ | mmap.PROT_WRITE,
                             offset=self.base)

    def read32(self, off):
        if off + 4 > self.size:
            raise ValueError("offset 0x%x past the 0x%x BAR" % (off, self.size))
        raw = int.from_bytes(self.map[off:off + 4], "big")
        return bswap32(raw)

    def write32(self, off, val):
        if off + 4 > self.size:
            raise ValueError("offset 0x%x past the 0x%x BAR" % (off, self.size))
        self.map[off:off + 4] = bswap32(val & 0xFFFFFFFF).to_bytes(4, "big")

    def close(self):
        self.map.close()
        os.close(self.fd)



# The 23 FE100 blocks, 0x8000 apart. Recovered from the vendor tooling and
# independently confirmed by reading every register in the map: exactly these
# 23 name prefixes appear, and all 5951 registers answer.
FE100_BLOCKS = {
    "hif": 0x000000, "tmi": 0x008000, "nif": 0x010000, "ipq": 0x018000,
    "par": 0x020000, "lif": 0x028000, "acl": 0x030000, "dfp": 0x038000,
    "flu": 0x040000, "cfp": 0x048000, "fwd": 0x050000, "lef": 0x058000,
    "qmm": 0x060000, "lag": 0x068000, "prw": 0x070000, "sem": 0x078000,
    "tlu": 0x080000, "egr": 0x088000, "fcm": 0x098000, "tdi": 0x0a0000,
    "fhm": 0x0a8000, "fdt": 0x0b0000, "prom": 0x0f8000,
}


def snapshot(fe, regmap, blocks=None):
    """Read every register (or every register of the named blocks) into a dict.

    THIS IS THE POINT OF THE TOOL, not a convenience.

    The FE100 bring-up sequence is known in SHAPE -- eleven read-modify-writes
    at 0x100 stride, waits, a PLL/link poll, a second RMW and poll -- but not in
    VALUES. An earlier attempt to read the offsets and values statically out of
    fe100_reg_wr produced numbers that match no register in the map, because
    that function is a logging wrapper whose arguments are biased into a string
    table. Those numbers were wrong and must not be reused.

    The reliable route is observation: snapshot the chip, let the vendor code
    run its init, snapshot again, and diff. That yields ordering AND values
    without having to interpret anyone's disassembly. This is the half of that
    which FFN can do on its own.
    """
    out = {}
    for name, r in regmap.items():
        if blocks and name.split("_")[0] not in blocks:
            continue
        try:
            out[name] = fe.read32(r["addr"])
        except ValueError:
            continue
    return out


def diff_snapshots(a, b):
    """Registers that differ, plus what appeared or vanished between reads."""
    changed, only_a, only_b = [], [], []
    for k in sorted(set(a) | set(b)):
        if k not in b:
            only_a.append(k)
        elif k not in a:
            only_b.append(k)
        elif a[k] != b[k]:
            changed.append((k, a[k], b[k]))
    return changed, only_a, only_b


def load_map(path):
    with open(path) as fh:
        return {r["name"]: r for r in json.load(fh)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--map", default="/opt/ffn/fe100-csr.json")
    ap.add_argument("--regs", nargs="*", help="register names to read")
    ap.add_argument("--block", help="read every register whose name starts with this")
    ap.add_argument("--limit", type=int, default=64)
    ap.add_argument("--nonzero", action="store_true",
                    help="only show registers that do not read 0")
    ap.add_argument("--write", nargs=2, metavar=("REG", "VALUE"),
                    help="write VALUE to REG (needs --allow-write)")
    ap.add_argument("--allow-write", action="store_true")
    ap.add_argument("--snapshot", metavar="FILE",
                    help="read every register and save it as JSON")
    ap.add_argument("--diff", nargs=2, metavar=("BEFORE", "AFTER"),
                    help="compare two snapshots and print what changed")
    ap.add_argument("--blocks", nargs="*",
                    help="restrict a snapshot to these block prefixes")
    args = ap.parse_args()

    # --diff reads no hardware, so it must not require the device -- the whole
    # point is to be able to analyse a capture somewhere else, later.
    if args.diff:
        with open(args.diff[0]) as fh:
            before = json.load(fh)
        with open(args.diff[1]) as fh:
            after = json.load(fh)
        changed, only_a, only_b = diff_snapshots(before, after)
        print("%d registers changed of %d compared"
              % (len(changed), len(set(before) & set(after))))
        for name, x, y in changed:
            print("  %-28s 0x%08x -> 0x%08x" % (name, x, y))
        for name in only_a:
            print("  %-28s  present before, missing after" % name)
        for name in only_b:
            print("  %-28s  missing before, present after" % name)
        return 0

    if not memory_decode_on():
        sys.stderr.write(
            "FE100 memory decode is OFF -- every read would return 0xffffffff\n"
            "and look like a register full of ones. Enable it with:\n"
            "  echo 1 > %s/enable\n" % SYSFS)
        return 2

    regmap = load_map(args.map)
    fe = Fe100()
    try:
        if args.snapshot:
            snap = snapshot(fe, regmap, set(args.blocks) if args.blocks else None)
            tmp = args.snapshot + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(snap, fh, indent=1, sort_keys=True)
            os.replace(tmp, args.snapshot)
            nz = sum(1 for v in snap.values() if v)
            print("snapshot: %d registers (%d non-zero) -> %s"
                  % (len(snap), nz, args.snapshot))
            return 0

        if args.write:
            if not args.allow_write:
                sys.stderr.write("refusing to write without --allow-write\n")
                return 2
            name, value = args.write
            if name not in regmap:
                sys.stderr.write("unknown register %r\n" % name)
                return 2
            fe.write32(regmap[name]["addr"], int(value, 0))
            print("%-28s <- 0x%08x  (readback 0x%08x)"
                  % (name, int(value, 0), fe.read32(regmap[name]["addr"])))
            return 0

        names = list(args.regs or [])
        if args.block:
            names += sorted(n for n in regmap if n.startswith(args.block))
        if not names:
            sys.stderr.write("nothing to read: pass --regs or --block\n")
            return 2

        shown = 0
        for n in names:
            if shown >= args.limit:
                print("  ... %d more not shown (--limit)" % (len(names) - shown))
                break
            r = regmap.get(n)
            if r is None:
                print("%-28s  UNKNOWN" % n)
                continue
            v = fe.read32(r["addr"])
            if args.nonzero and v == 0:
                continue
            print("%-28s +0x%05x = 0x%08x" % (n, r["addr"], v))
            shown += 1
    finally:
        fe.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
