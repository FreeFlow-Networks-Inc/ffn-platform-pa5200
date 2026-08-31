#!/usr/bin/env python3
"""ffn_octprobe -- talk to the OCTEON endpoint directly, read-only by default.

Why this exists
---------------
FFN's bring-up plan assumed a working BAR window into Octeon DRAM. On the live
PA-5220 that assumption was wrong for a mundane reason: vfio-pci parks a device
nobody has opened in **D3hot**, so every BAR read came back all-0xff and looked
like dead silicon. `bind_vfio` binds the endpoint but nothing ever opens it.

Opening the device through vfio (which is all this tool does) brings it to D0,
and the picture changes:

    BAR0  8 MiB   responds -- live register window
    BAR2  64 MiB  all-0xff -- DRAM window, not mapped until DRAM is initialised
    BAR4  1 MiB   all-0xff

So "which BAR is Octeon DRAM" is answerable by evidence rather than by the
size guess that ffn_oct.py rightly refuses to make.

Two ways to reach the chip, both found in the vendor bootloader:
  * the PCIe register window -- this tool
  * the **PCI console**, gated by the u-boot env var `pci_console_active`
    ("PCI console output has been enabled."), present in both the CP and DP
    bootloaders. That is a bidirectional console over PCIe, so bring-up need
    not be blind. Its descriptor is reached through the `__common_bootinfo`
    named bootmem block; FFN cannot resolve that yet (see ffn_oct.py step 7).

Safety
------
Reads only. It never writes a BAR or a CSR. Opening a vfio device does enable
it and vfio-pci may reset the function on open/close -- acceptable because the
endpoint is idle and a slot reset is step 1 of the bring-up anyway. Anything
that writes must be a separate, explicit tool.
"""
import fcntl
import os
import struct
import sys

VFIO_GET_API_VERSION = 0x3B64
VFIO_CHECK_EXTENSION = 0x3B65
VFIO_SET_IOMMU = 0x3B66
VFIO_GROUP_GET_STATUS = 0x3B67
VFIO_GROUP_SET_CONTAINER = 0x3B68
VFIO_GROUP_GET_DEVICE_FD = 0x3B6A
VFIO_DEVICE_GET_INFO = 0x3B6B
VFIO_DEVICE_GET_REGION_INFO = 0x3B6C

VFIO_TYPE1_IOMMU = 1
VFIO_GROUP_FLAGS_VIABLE = 1

PCI_VENDOR_CAVIUM = "177d"
SYSFS = "/sys/bus/pci/devices"


def res_kind(idx):
    """Linux's resource array is not BAR0..BARn: 0-5 are the real BARs, 6 is
    the expansion ROM, 7-10 are bridge windows, 11+ are SR-IOV."""
    if idx <= 5:
        return "bar"
    if idx == 6:
        return "rom"
    if idx <= 10:
        return "bridge-window"
    return "iov"


def octeon_endpoints():
    out = []
    for d in sorted(os.listdir(SYSFS)):
        p = os.path.join(SYSFS, d)
        try:
            with open(os.path.join(p, "vendor")) as f:
                if f.read().strip().replace("0x", "").lower() != PCI_VENDOR_CAVIUM:
                    continue
        except OSError:
            continue
        drv = ""
        try:
            drv = os.path.basename(os.readlink(os.path.join(p, "driver")))
        except OSError:
            pass
        grp = ""
        try:
            grp = os.path.basename(os.readlink(os.path.join(p, "iommu_group")))
        except OSError:
            pass
        out.append({"pci": d, "driver": drv, "group": grp,
                    "power": _read(os.path.join(p, "power_state")),
                    "enabled": _read(os.path.join(p, "enable"))})
    return out


def _read(p, default="?"):
    try:
        with open(p) as f:
            return f.read().strip()
    except OSError:
        return default


class VfioDevice:
    """Just enough vfio to open a function and read its regions."""

    def __init__(self, pci, group):
        self.pci = pci
        self.group_path = "/dev/vfio/%s" % group
        self.cont = self.grp = self.dev = None

    def __enter__(self):
        self.cont = os.open("/dev/vfio/vfio", os.O_RDWR)
        if fcntl.ioctl(self.cont, VFIO_GET_API_VERSION) != 0:
            raise RuntimeError("unexpected vfio API version")
        if not fcntl.ioctl(self.cont, VFIO_CHECK_EXTENSION, VFIO_TYPE1_IOMMU):
            raise RuntimeError("TYPE1 iommu unavailable; is intel_iommu=on?")
        self.grp = os.open(self.group_path, os.O_RDWR)
        b = bytearray(struct.pack("II", 8, 0))
        fcntl.ioctl(self.grp, VFIO_GROUP_GET_STATUS, b, True)
        _, flags = struct.unpack("II", bytes(b))
        if not flags & VFIO_GROUP_FLAGS_VIABLE:
            raise RuntimeError("iommu group %s is not viable: every device in "
                               "it must be bound to vfio-pci" % self.group_path)
        fcntl.ioctl(self.grp, VFIO_GROUP_SET_CONTAINER, struct.pack("i", self.cont))
        fcntl.ioctl(self.cont, VFIO_SET_IOMMU, VFIO_TYPE1_IOMMU)
        self.dev = fcntl.ioctl(self.grp, VFIO_GROUP_GET_DEVICE_FD,
                               bytearray(self.pci.encode() + b"\0"), True)
        return self

    def __exit__(self, *a):
        for fd in (self.dev, self.grp, self.cont):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def info(self):
        b = bytearray(struct.pack("IIII", 16, 0, 0, 0))
        fcntl.ioctl(self.dev, VFIO_DEVICE_GET_INFO, b, True)
        _, flags, nregions, nirqs = struct.unpack("IIII", bytes(b))
        return {"flags": flags, "regions": nregions, "irqs": nirqs}

    def region(self, idx):
        b = bytearray(struct.pack("IIIIQQ", 32, 0, idx, 0, 0, 0))
        try:
            fcntl.ioctl(self.dev, VFIO_DEVICE_GET_REGION_INFO, b, True)
        except OSError:
            return None
        _, flags, ridx, _cap, size, offset = struct.unpack("IIIIQQ", bytes(b))
        if not size:
            return None
        return {"index": ridx, "flags": flags, "size": size, "offset": offset,
                "read": bool(flags & 1), "write": bool(flags & 2),
                "mmap": bool(flags & 4)}

    def read(self, reg, off, length=8):
        return os.pread(self.dev, length, reg["offset"] + off)


ALL_ONES = bytes([255]) * 8
DEAD = (b"\xff" * 8, b"\x00" * 8)


def map_region(dv, reg, step, limit):
    """Sample a region and report which spans answer with something other than
    all-ones. All-ones is a PCIe read that went nowhere."""
    live, blank, dead = [], 0, 0
    end = min(reg["size"], limit)
    off = 0
    while off < end:
        try:
            raw = dv.read(reg, off)
        except OSError:
            dead += 1
            off += step
            continue
        if raw == b"\xff" * 8:
            dead += 1
        elif raw == b"\x00" * 8:
            blank += 1
        else:
            live.append((off, raw))
        off += step
    return live, blank, dead


def main():
    a = sys.argv[1:]
    eps = octeon_endpoints()
    if not eps:
        print("no Cavium/OCTEON endpoint on this host")
        return 1

    print("=== OCTEON endpoints ===")
    for e in eps:
        print("  %s  driver=%-9s group=%-4s power=%-6s enabled=%s"
              % (e["pci"], e["driver"] or "-", e["group"] or "-",
                 e["power"], e["enabled"]))
    print()
    if "--list" in a:
        return 0

    want = None
    if "--pci" in a:
        try:
            want = a[a.index("--pci") + 1]
        except IndexError:
            pass
    target = None
    for e in eps:
        if want is None or e["pci"] == want:
            target = e
            break
    if not target:
        print("no such endpoint %r" % want)
        return 1
    if target["driver"] != "vfio-pci":
        print("%s is bound to %r, not vfio-pci -- bind it first"
              % (target["pci"], target["driver"]))
        return 1

    step = 0x1000
    if "--step" in a:
        try:
            step = int(a[a.index("--step") + 1], 0)
        except (IndexError, ValueError):
            pass
    limit = 1 << 20
    if "--limit" in a:
        try:
            limit = int(a[a.index("--limit") + 1], 0)
        except (IndexError, ValueError):
            pass

    with VfioDevice(target["pci"], target["group"]) as dv:
        nfo = dv.info()
        print("opened %s: regions=%d irqs=%d flags=0x%x"
              % (target["pci"], nfo["regions"], nfo["irqs"], nfo["flags"]))
        print("power now %s, enabled %s  (opening the device is what brings it "
              "out of D3hot)"
              % (_read(os.path.join(SYSFS, target["pci"], "power_state")),
                 _read(os.path.join(SYSFS, target["pci"], "enable"))))
        print()
        if "--dump" in a:
            try:
                bidx = int(a[a.index("--dump") + 1], 0)
                at = int(a[a.index("--dump") + 2], 0)
                ln = int(a[a.index("--dump") + 3], 0)
            except (IndexError, ValueError):
                print("usage: --dump <bar> <offset> <length>")
                return 2
            reg = dv.region(bidx)
            if not reg:
                print("BAR%d has no region" % bidx)
                return 1
            print("=== BAR%d dump 0x%x..0x%x ===" % (bidx, at, at + ln))
            off = at
            while off < at + ln:
                try:
                    raw = dv.read(reg, off)
                except OSError as e:
                    print("  0x%06x: read failed: %s" % (off, e))
                    off += 8
                    continue
                v = struct.unpack(">Q", raw)[0]
                tag = ""
                if raw == ALL_ONES:
                    tag = "  (no response)"
                print("  0x%06x: %016x%s" % (off, v, tag))
                off += 8
            return 0

        for idx in range(min(nfo["regions"], 6)):
            reg = dv.region(idx)
            if not reg:
                continue
            live, blank, dead = map_region(dv, reg, step, limit)
            total = len(live) + blank + dead
            verdict = ("DEAD (every read all-ones -- nothing is answering)"
                       if not live and not blank else
                       "LIVE" if live else
                       "answering, all zero")
            print("=== BAR%d  %7.2f MiB  r=%d w=%d mmap=%d  -- %s"
                  % (reg["index"], reg["size"] / (1 << 20), reg["read"],
                     reg["write"], reg["mmap"], verdict))
            print("    sampled %d x 8B every 0x%x over the first %.2f MiB: "
                  "%d non-trivial, %d zero, %d all-ones"
                  % (total, step, min(reg["size"], limit) / (1 << 20),
                     len(live), blank, dead))
            for off, raw in live[:12]:
                print("      0x%06x: %s" % (off, raw.hex()))
            if len(live) > 12:
                print("      ... %d more" % (len(live) - 12))
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
