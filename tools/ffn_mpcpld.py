#!/usr/bin/env python3
"""MP CPLD on the host's LPC bus -- read, and optionally pulse the DP/FE100
reset the way PAN's masterd does.

How it was found (no guessing at I/O ports):
  * PAN reaches it as `/dev/cpld`; `lpc_cpld_reg_write(reg,val)` is
    open64(/dev/cpld) -> lseek(reg) -> write(1 byte), in
    libpancommon_mp.so. The driver behind /dev/cpld is built into PAN's kernel, so
    there is no module to borrow -- FFN must reach the hardware itself.
  * It is an LPC device, so the PCH's own Generic I/O Decode Ranges say where:
        LGIR1 0x000c0ca1 -> 0x0ca0..0x0caf   (reads all-ff, nothing there)
        LGIR2 0x00fc0601 -> 0x0600..0x06ff   (256 bytes)
    GPIOBASE is 0x500 and PMBASE 0x400, so neither collides.
  * 0x0600 reads 0x50 0x41 = ASCII "PA". That is the CPLD.

What masterd does, in its first start-script for the 5200 family: it asserts
the DP and FE100 reset by setting bit 0 of CPLD register 0x4, holds that for one
second, clears the bit, then waits another second before continuing.
DELIBERATE DEVIATION: PAN writes the whole byte because its comment says "MP
CPLD read is not implemented yet (so reads 0xff)". On this board reads clearly
DO work (we get "PA", and reg4 = 0x08), so this does a read-modify-write on
bit 0 only and preserves bit 3, whose meaning is unknown. Blindly zeroing an
unknown bit on a live firewall is the riskier choice, not the faithful one.
"""
import os
import sys
import time

PORT = "/dev/port"
CPLD_BASE = 0x0600
# 64 registers, not 32. The vendor's own host-side PDT module for this CPLD
# clamps any register dump to 64 entries and documents register 0xf as the CPLD
# version. Live silicon reads 0x05 there, which cross-validates that map
# against this board. FFN was reading only the low half, so registers
# 0x20-0x3f were never looked at.
CPLD_LEN = 0x40
VERSION_REG = 0x0F          # per PAN's lpc_cpld.py show.version()
RESET_REG = 0x04
RESET_BIT = 0x01

# The full 64-register map, read on live silicon and stable across two passes.
# This closes the open question above about registers 0x20-0x3f: there is
# nothing in them.
#
#   00: 50 41 00 de 08 de de de de de de de de de de 05
#   10: de de de de de de de de de de de de de 00 00 de
#   20: de de de de de de de de de de de de de de de de
#   30: de de de de de de de de de de de de de de de de
#
# 0xde is this CPLD's "no register here" value -- NOT 0xff, which is what a
# floating bus would normally give and what PAN's own comment assumed reads
# would return. That single observation decodes the whole map, because every
# real register is simply the ones that are not 0xde:
#
#   0x00, 0x01   "PA" signature
#   0x02         0x00, real, meaning unknown
#   0x04         reset control. bit 0 = DP + FE100 reset (0 = deasserted).
#                bit 3 reads 1 and its meaning is unknown, which is why this
#                tool read-modify-writes rather than writing the whole byte.
#   0x0f         0x05, version -- matches PAN's documented version register,
#                which cross-validates the map against this board
#   0x1d, 0x1e   0x00, real, meaning unknown
#   everything else   unimplemented
#
# So the CPLD is small: a signature, one reset control, a version, and three
# registers whose purpose is not established. There is no second bank to find,
# and no per-device reset or power control beyond bit 0 of 0x04.
#
# Worth knowing for the BCM88375 work: this is the only hardware reset of the
# dataplane complex reachable from the MP, i.e. the way to get a clean chip
# without physically power-cycling the appliance. Two caveats. Whether the
# BCM88375 is inside this reset domain is UNVERIFIED -- it is documented as
# "DP + FE100" and the BCM hangs off the CP Octeon's PCIe. And pulsing it takes
# the CP down, which is FFN's own access path, so recovery means re-running the
# Octeon bring-up. It is a deliberate operation, not a casual one.
#
# There is no IPMI or BMC on this board, before anyone looks: no /dev/ipmi*, no
# ipmi modules, no /sys/class/ipmi, and `dmidecode -t 38` (IPMI Device
# Information) returns empty. Board management is this CPLD plus /dev/i2c-0
# (Intel i801 SMBus, unexplored, likely sensors).


def rd(off):
    fd = os.open(PORT, os.O_RDONLY)
    try:
        os.lseek(fd, CPLD_BASE + off, 0)
        b = os.read(fd, 1)
        return b[0] if b else None
    finally:
        os.close(fd)


def wr(off, val):
    fd = os.open(PORT, os.O_WRONLY)
    try:
        os.lseek(fd, CPLD_BASE + off, 0)
        return os.write(fd, bytes([val & 0xFF])) == 1
    finally:
        os.close(fd)


def dump(n=CPLD_LEN):
    return [rd(i) for i in range(n)]


def show(label, regs):
    """Print registers as offset-labelled rows of 16."""
    for base in range(0, len(regs), 16):
        row = regs[base:base + 16]
        print("  %-8s %02x: %s" % (label if base == 0 else "", base,
                                   " ".join("%02x" % (v if v is not None
                                                      else 0xFF)
                                            for v in row)))


print("=== MP CPLD @ I/O 0x%03x ===" % CPLD_BASE)
a = dump()
time.sleep(0.3)
b = dump()
show("read1", a)
show("read2", b)
stable = a == b
print("  reads are %s" % ("STABLE" if stable else "UNSTABLE -- not writing"))
ver = a[VERSION_REG]
print("  reg 0x%02x (version, per PAN lpc_cpld.py) = %s"
      % (VERSION_REG, "0x%02x" % ver if ver is not None else "unreadable"))
sig = bytes(a[0:2])
print("  signature reg0..1 = %r %s" % (sig, "(PA = Palo Alto)"
                                       if sig == b"PA" else "(unexpected)"))
print("  reg%d (DP+FE100 reset) = 0x%02x   reset bit 0 = %d"
      % (RESET_REG, a[RESET_REG], a[RESET_REG] & RESET_BIT))

if not stable or sig != b"PA":
    print("\nrefusing to write: this does not look like the MP CPLD")
    sys.exit(1)

if "--pulse" not in sys.argv:
    print()
    print("read-only. --pulse would, preserving every other bit:")
    print("  1. set   reg4 bit0 -> 0x%02x   (assert DP + FE100 reset)"
          % (a[RESET_REG] | RESET_BIT))
    print("  2. sleep 1")
    print("  3. clear reg4 bit0 -> 0x%02x   (deassert / release)"
          % (a[RESET_REG] & ~RESET_BIT))
    print("  4. sleep 1, then verify")
    sys.exit(0)

base = a[RESET_REG]
print()
print("=== pulsing the DP + FE100 reset ===")
print("  assert:  reg4 0x%02x -> 0x%02x" % (base, base | RESET_BIT))
wr(RESET_REG, base | RESET_BIT)
time.sleep(1)
mid = rd(RESET_REG)
print("  reads back 0x%02x  (reset asserted = %d)" % (mid, mid & RESET_BIT))

print("  deassert: reg4 0x%02x -> 0x%02x" % (mid, mid & ~RESET_BIT))
wr(RESET_REG, mid & ~RESET_BIT)
time.sleep(1)
end = rd(RESET_REG)
print("  reads back 0x%02x  (reset asserted = %d)" % (end, end & RESET_BIT))

print()
print("=== after ===")
show("regs", dump())
print()
if end is not None and not (end & RESET_BIT):
    print("DP + FE100 reset is DEASSERTED -- they are released.")
else:
    print("reset still reads asserted; the write may not have taken.")
