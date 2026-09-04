#!/usr/bin/env python3
"""ffn_fe100probe -- does the FE100 answer on the CP?

The FE100 is a Palo Alto device, NOT the BCM88375 (that is the Dune Petra part
at 14e4:8375). PAN's own PDT ships separate modules and libraries for the two:
fe100.py -> condorlib, petra.py -> petralib. 0xfeed is PAN's PCI vendor ID.

    0002:01:00.0   feed:fe1c   class 0x020000 (Ethernet)
                   BAR0 1 MB   driver: none

Read-only by design. It maps BAR0 and reads registers whose offsets come from
the CSR map recovered from libpandp_cp.so DWARF (fe100-csr-map.txt, 5951
registers) -- so every address here is hardware description, not a guess.

prom_chip_rev_num is at 0xffffc, the last dword of a 1 MB space, which is itself
a corroboration that this BAR is the space that map describes.

Nothing is written. The device is left exactly as found, including its PCI
COMMAND register: enabling memory decode is a side effect worth doing
deliberately, not as part of a probe.
"""
import mmap
import os
import struct
import sys

DEV = os.environ.get("FFN_FE100_PCI", "0002:01:00.0")
SYS = "/sys/bus/pci/devices/" + DEV

# offset, name -- all from the recovered CSR map
REGS = [
    (0xffffc, "prom_chip_rev_num"),
    (0x30000, "acl_intr_enable"),
]


def rd_cfg(off, size):
    with open(SYS + "/config", "rb") as f:
        f.seek(off)
        b = f.read(size)
    return int.from_bytes(b, "little")


def main():
    if not os.path.isdir(SYS):
        print("no such PCI device: %s" % DEV)
        return 2

    vend = rd_cfg(0x00, 2)
    devid = rd_cfg(0x02, 2)
    cmd = rd_cfg(0x04, 2)
    klass = open(SYS + "/class").read().strip()
    drv = os.path.basename(os.path.realpath(SYS + "/driver")) if os.path.exists(SYS + "/driver") else "none"

    print("  device   %s  %04x:%04x  class %s  driver %s" % (DEV, vend, devid, klass, drv))
    print("  PCI_COMMAND 0x%04x  (mem-decode bit1 = %d, busmaster bit2 = %d)"
          % (cmd, (cmd >> 1) & 1, (cmd >> 2) & 1))

    res = SYS + "/resource0"
    size = os.path.getsize(res)
    print("  resource0 %d bytes (%d KB)" % (size, size // 1024))

    if not (cmd >> 1) & 1:
        print("  NOTE: memory decode is OFF, so reads will return all-ones.")
        print("        Enable it deliberately, not from a probe:")
        print("          setpci -s %s COMMAND=0x%04x" % (DEV, cmd | 0x2))

    try:
        fd = os.open(res, os.O_RDONLY)
    except OSError as e:
        print("  cannot open resource0: %s" % e)
        return 1
    try:
        m = mmap.mmap(fd, size, prot=mmap.PROT_READ)
    except OSError as e:
        print("  cannot mmap resource0: %s" % e)
        os.close(fd)
        return 1

    print("  --- register reads (offsets from the DWARF-recovered CSR map) ---")
    allones = 0
    for off, name in REGS:
        if off + 4 > size:
            print("    %-22s 0x%06x  OUT OF RANGE for this BAR" % (name, off))
            continue
        val = struct.unpack("<I", m[off:off + 4])[0]
        beval = struct.unpack(">I", m[off:off + 4])[0]
        note = ""
        if val == 0xffffffff:
            allones += 1
            note = "  <- all ones: not decoding / no response"
        elif val == 0:
            note = "  <- zero (reset value per the map)"
        print("    %-22s 0x%06x  le=0x%08x  be=0x%08x%s" % (name, off, val, beval, note))

    m.close()
    os.close(fd)

    if allones == len(REGS):
        print("  VERDICT: no response. Check memory decode, then the reset in MP")
        print("           CPLD reg 0x4 bit 0 (asserts DP+FE100 reset).")
    else:
        print("  VERDICT: the FE100 register space RESPONDS.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
