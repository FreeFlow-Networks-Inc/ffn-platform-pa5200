#!/usr/bin/env python3
"""
ffn_nacscan -- map the CA1 (NAC) register space through /dev/ffn_nacN.

This is the instrument that goes with the ffn_ca1 driver. We do NOT have the
vendor's register map for this device, so rather than guess offsets we find
the live registers empirically, the same way the chassis CPLD map was found:

  * dump      -- raw hex of a region
  * live      -- which words are neither 0x00000000 nor 0xffffffff, i.e. the
                 addresses the device actually decodes
  * volatile  -- re-read the live set and report which words CHANGE on their
                 own (counters, status, free-running timers). A changing
                 register is the strongest evidence a block is running.

Read-only. The driver refuses writes unless loaded with allow_write=1, and
this tool never writes.

Runs on the CP (mips64). The CP has CPython but no compiler, which is why this
is Python and not a C utility.

Usage:
    ffn_nacscan.py info
    ffn_nacscan.py dump  [--bar N] [--off 0] [--len 256]
    ffn_nacscan.py live  [--bar N]
    ffn_nacscan.py volatile [--bar N] [--passes 3] [--delay 0.5]
"""

import argparse
import fcntl
import os
import struct
import sys
import time

DEV_DEFAULT = "/dev/ffn_nac0"

# Must track ffn_nac_abi.h.
SLOT_SHIFT = 22
SLOT_SIZE = 1 << SLOT_SHIFT
NUM_BARS = 4

# struct ffn_nac_info head is padded to 24 bytes so bar[] lands 8-aligned:
#   u32 abi, u16 ven, u16 dev, u8 rev, u8 nbars, u8 wen, u8 pad0,
#   u32 cmd, u32 caps, u32 pad2      -> 24 bytes
# then NUM_BARS x (u64 phys, u64 len, u64 slot_off, u32 flags, u32 pad) = 32 each,
# then char variant[16]. Verified by offsetof on BOTH x86-64 and mips64 BE:
# bar at 24, variant at 152, total 168 -- no implicit padding on either.
# The CP is big-endian MIPS64, so use native ("=") order, not little-endian.
_INFO_HEAD = "=IHHBBBBIII"
_INFO_BAR = "=QQQII"
_VARIANT_MAX = 16
_INFO_SIZE = (struct.calcsize(_INFO_HEAD)
              + NUM_BARS * struct.calcsize(_INFO_BAR)
              + _VARIANT_MAX)

# Capability bits, each measured from the vendor module that owns the id.
CAPS = ((1 << 0, "ddr-layouts"),   # nac_ddr_layout_small/medium/large
        (1 << 1, "mem-check"),     # "<NAME> memory check failed"
        (1 << 2, "rld-cal"),       # check_rld_status(), CE40 only
        (1 << 3, "rld-retry"))     # RLDRAM init retry, ocelot only

# ioctl request encoding.
#
# MIPS does NOT use the asm-generic encoding. It follows the sparc/alpha
# family: _IOC_SIZEBITS is 13 (not 14), _IOC_DIRBITS is 3 (not 2), the
# direction field therefore starts at bit 29 (not 30), and the direction
# values are NONE=1, READ=2, WRITE=4 (not 0/1/2). Encoding this the generic
# way yields a request number the driver rejects with ENOTTY.
if os.uname().machine.startswith("mips"):
    _IOC_SIZESHIFT, _IOC_DIRSHIFT, _IOC_READ = 16, 29, 2
else:
    _IOC_SIZESHIFT, _IOC_DIRSHIFT, _IOC_READ = 16, 30, 2

FFN_NAC_IOC_INFO = ((_IOC_READ << _IOC_DIRSHIFT) |
                    (_INFO_SIZE << _IOC_SIZESHIFT) |
                    (ord("C") << 8) | 1)


def get_info(fd):
    buf = bytearray(_INFO_SIZE)
    fcntl.ioctl(fd, FFN_NAC_IOC_INFO, buf, True)
    head = struct.unpack_from(_INFO_HEAD, buf, 0)
    off = struct.calcsize(_INFO_HEAD)
    bars = []
    for _ in range(NUM_BARS):
        bars.append(struct.unpack_from(_INFO_BAR, buf, off))
        off += struct.calcsize(_INFO_BAR)
    variant = bytes(buf[off:off + _VARIANT_MAX]).split(b"\x00")[0].decode()
    return {
        "abi": head[0], "vendor": head[1], "device": head[2],
        "revision": head[3], "num_bars": head[4], "write_enabled": head[5],
        "caps": head[8], "variant": variant,
        "pci_command": head[7],
        "bars": [{"phys": b[0], "len": b[1], "slot_off": b[2], "flags": b[3]}
                 for b in bars],
    }


def read_region(fd, flat_off, length):
    """Read `length` bytes at flat offset `flat_off`. Returns bytes or None."""
    out = bytearray()
    remaining = length
    pos = flat_off
    while remaining:
        chunk = min(remaining, 4096)          # driver's per-call bounce size
        os.lseek(fd, pos, os.SEEK_SET)
        try:
            data = os.read(fd, chunk)
        except OSError as e:
            print("  read at 0x%x failed: %s" % (pos, e), file=sys.stderr)
            return None
        if not data:
            break
        out += data
        pos += len(data)
        remaining -= len(data)
    return bytes(out)


def words(data, big_endian):
    fmt = ">I" if big_endian else "<I"
    return [struct.unpack_from(fmt, data, i)[0]
            for i in range(0, len(data) - 3, 4)]


def cmd_info(fd, args):
    i = get_info(fd)
    caps = " ".join(n for b, n in CAPS if i["caps"] & b) or "none"
    print("ABI v%d  %s  %04x:%04x rev %02x  COMMAND=0x%04x  writes=%s"
          % (i["abi"], i["variant"] or "?", i["vendor"], i["device"],
             i["revision"], i["pci_command"],
             "ENABLED" if i["write_enabled"] else "denied"))
    print("  capabilities: %s" % caps)
    mem = "ON" if i["pci_command"] & 0x2 else "OFF"
    bm = "ON" if i["pci_command"] & 0x4 else "off"
    print("  memory decode %s, bus master %s" % (mem, bm))
    for n, b in enumerate(i["bars"]):
        if not b["len"]:
            print("  BAR%d  absent" % n)
            continue
        print("  BAR%d  phys 0x%012x  len %6d KiB  chardev offset 0x%07x"
              % (n, b["phys"], b["len"] // 1024, b["slot_off"]))


def _bar_list(info, want):
    if want is not None:
        return [want]
    return [n for n, b in enumerate(info["bars"]) if b["len"]]


def cmd_dump(fd, args):
    info = get_info(fd)
    for n in _bar_list(info, args.bar):
        b = info["bars"][n]
        if not b["len"]:
            continue
        length = min(args.len, b["len"] - args.off)
        data = read_region(fd, b["slot_off"] + args.off, length)
        if data is None:
            continue
        print("=== BAR%d +0x%x, %d bytes ===" % (n, args.off, len(data)))
        for row in range(0, len(data), 16):
            chunk = data[row:row + 16]
            hexs = " ".join("%02x" % c for c in chunk)
            print("  %08x: %-47s" % (args.off + row, hexs))


def probe_alive(fd, slot_off):
    """
    Read ONE word. Returns (ok, value).

    Always do this before any larger scan. When the device is not answering,
    every access master-aborts and costs a full PCIe completion timeout --
    measured at ~21 ms per 32-bit word on this path. Sweeping a 4 MiB BAR at
    that rate would take about six hours, so a dead window must be detected
    from a single access, not discovered part-way through a sweep.
    """
    os.lseek(fd, slot_off, os.SEEK_SET)
    try:
        d = os.read(fd, 4)
    except OSError:
        return False, None
    if len(d) < 4:
        return False, None
    return True, struct.unpack("=I", d)[0]


def cmd_live(fd, args):
    """Sample the window and report words that are neither 0x0 nor 0xffffffff."""
    info = get_info(fd)
    for n in _bar_list(info, args.bar):
        b = info["bars"][n]
        if not b["len"]:
            continue

        ok, first = probe_alive(fd, b["slot_off"])
        if not ok:
            print("=== BAR%d: unreadable ===" % n)
            continue
        if first == 0xffffffff:
            print("=== BAR%d: NOT ANSWERING (first word all-ones) -- skipping;"
                  " a sweep here would take hours ===" % n)
            continue

        # Alive: sample at a stride so cost stays bounded even if parts of the
        # window are dark.
        span = min(args.len, b["len"]) if args.len else min(b["len"], 65536)
        stride = max(4, args.stride)
        found = []
        for off in range(0, span, stride):
            os.lseek(fd, b["slot_off"] + off, os.SEEK_SET)
            try:
                d = os.read(fd, 4)
            except OSError:
                break
            if len(d) < 4:
                break
            # The driver returns ioread32() results in CPU order, so native
            # ("=") is the authoritative value; the byte-swap is shown only as
            # a hint when a field looks like it might be the other way round.
            v = struct.unpack("=I", d)[0]
            sw = struct.unpack("=I", d[::-1])[0]
            if v not in (0x00000000, 0xffffffff):
                found.append((off, v, sw))
            if len(found) >= args.max:
                break
        print("=== BAR%d: %d live words sampling %d KiB every %d bytes ==="
              % (n, len(found), span // 1024, stride))
        for off, v, sw in found:
            print("  +0x%06x  %08x   (byteswapped %08x)" % (off, v, sw))


def cmd_volatile(fd, args):
    """Re-read and report words that change by themselves."""
    info = get_info(fd)
    for n in _bar_list(info, args.bar):
        b = info["bars"][n]
        if not b["len"]:
            continue
        ok, first = probe_alive(fd, b["slot_off"])
        if not ok or first == 0xffffffff:
            print("=== BAR%d: not answering -- skipping ===" % n)
            continue
        span = min(args.len, b["len"]) if args.len else min(b["len"], 4096)
        snaps = []
        for p in range(args.passes):
            data = read_region(fd, b["slot_off"], span)
            if data is None:
                break
            snaps.append(words(data, False))
            if p + 1 < args.passes:
                time.sleep(args.delay)
        if len(snaps) < 2:
            continue
        changing = [i for i in range(len(snaps[0]))
                    if any(s[i] != snaps[0][i] for s in snaps[1:])]
        print("=== BAR%d: %d changing words over %d passes (%.1fs apart) ==="
              % (n, len(changing), len(snaps), args.delay))
        for i in changing[:args.max]:
            vals = " -> ".join("%08x" % s[i] for s in snaps)
            print("  +0x%06x  %s" % (i * 4, vals))
        if len(changing) > args.max:
            print("  ... %d more" % (len(changing) - args.max))


def main():
    ap = argparse.ArgumentParser(description="map the CA1 NAC register space")
    ap.add_argument("cmd", choices=["info", "dump", "live", "volatile"])
    ap.add_argument("--dev", default=DEV_DEFAULT)
    ap.add_argument("--bar", type=int, default=None, help="limit to one BAR")
    ap.add_argument("--off", type=lambda s: int(s, 0), default=0)
    ap.add_argument("--len", type=lambda s: int(s, 0), default=256)
    ap.add_argument("--max", type=int, default=64, help="max lines per BAR")
    ap.add_argument("--stride", type=lambda s: int(s, 0), default=4,
                    help="bytes between samples in 'live' (default 4 = every "
                         "word; raise it to cover a wide window cheaply)")
    ap.add_argument("--passes", type=int, default=3)
    ap.add_argument("--delay", type=float, default=0.5)
    args = ap.parse_args()

    if args.cmd in ("live", "volatile") and args.len == 256:
        args.len = 0            # whole BAR by default for the scans

    try:
        fd = os.open(args.dev, os.O_RDONLY)
    except OSError as e:
        print("cannot open %s: %s" % (args.dev, e), file=sys.stderr)
        print("is ffn_nac.ko loaded, and is the CP the machine you are on?",
              file=sys.stderr)
        return 1
    try:
        {"info": cmd_info, "dump": cmd_dump,
         "live": cmd_live, "volatile": cmd_volatile}[args.cmd](fd, args)
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
