#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""ffn_mipscall.py -- name the `jalr t9` call targets in a MIPS64 shared object.

Reading the vendor's OCTEON libraries is how most of this platform got
documented, and the thing that repeatedly blocks it is that objdump shows every
inter-library call as an anonymous `jalr t9`. The callee is reachable only
through the GOT, and MIPS deliberately carries NO explicit relocation for a
global GOT entry -- that is the point of DT_MIPS_GOTSYM -- so `objdump -R` has
nothing to say either. Whole init sequences read as a wall of unnamed calls.

The mapping is mechanical, though, and comes from three dynamic tags:

    gp     = DT_PLTGOT + 0x7ff0
    slot   = gp + <the offset in `ld t9,OFF(gp)`>
    symbol = DT_MIPS_GOTSYM + (slot - DT_PLTGOT)/8 - DT_MIPS_LOCAL_GOTNO

THE TRAP, which cost a wrong answer before it was found: .dynsym must be indexed
by its own `Num:` column, not by the order surviving lines arrive in. Entry 0 has
no name, so a parser that appends only named lines shifts every later index by
one and still produces confident, plausible, wrong output -- the first version of
this reported `e2s_temod_ref_clk_t`, a TYPE, as a called function.

So it is calibrated, and stays calibrated: --selftest re-derives three calls in
gryphon_read_sfp_state whose identity is independently fixed by their argument
shapes -- libi2c_open(bus, &h), libi2c_read_cmd_byte(&h, addr, off) twice,
libi2c_close(&h). If that does not reproduce, do not trust anything else here.

Unlike tools/ffn_gotstr.py and tools/ffn_findsym.py, which hardcode one gp for
one function in one binary, this derives everything from the file.

Usage:
    ffn_mipscall.py <elf> <low-addr> <high-addr>
    ffn_mipscall.py --selftest <libports.so>
"""
import argparse
import re
import subprocess
import sys

# The OCTEON SDK cross-binutils read these big-endian MIPS64 objects with line
# numbers; the host's native objdump does not. Overridable for a different tree.
DEFAULT_PREFIX = ("/mnt/clones/sdk51/OCTEON-SDK/tools-gcc-4.7/bin/"
                  "mips64-octeon-linux-gnu-")


def run(prefix, tool, *args):
    return subprocess.run([prefix + tool] + list(args),
                          capture_output=True, text=True).stdout


class Resolver:
    def __init__(self, elf, prefix=DEFAULT_PREFIX):
        self.elf, self.prefix = elf, prefix
        dyn = run(prefix, "readelf", "-d", elf)

        def tag(name):
            m = re.search(r"\(" + name + r"\)\s+(0x[0-9a-fA-F]+|\d+)", dyn)
            if not m:
                raise SystemExit("%s: no DT_%s -- not a MIPS shared object?"
                                 % (elf, name))
            return int(m.group(1), 0)

        self.got = tag("PLTGOT")
        self.gotsym = tag("MIPS_GOTSYM")
        self.localno = tag("MIPS_LOCAL_GOTNO")
        self.gp = self.got + 0x7FF0

        # Index by the Num: column. See THE TRAP above.
        self.syms = {}
        for line in run(prefix, "readelf", "--dyn-syms", "-W", elf).splitlines():
            m = re.match(r"\s*(\d+):\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+(\S+)",
                         line)
            if m:
                self.syms[int(m.group(1))] = m.group(2).split("@")[0]

    def name_of(self, off):
        slot = self.gp + off
        idx = self.gotsym + (slot - self.got) // 8 - self.localno
        return self.syms.get(idx, "<unresolved: slot %#x, sym idx %d>"
                             % (slot, idx))

    def calls(self, lo, hi):
        """Yield ('src', text) and ('call', name, argstr) in program order."""
        dis = run(self.prefix, "objdump", "-d", "-l",
                  "--start-address=%#x" % lo, "--stop-address=%#x" % hi,
                  self.elf)
        pend, lastsrc, args = None, None, {}
        for line in dis.splitlines():
            if line.startswith("/"):
                src = re.sub(r"^.*?/src/", "", line).strip()
                if src != lastsrc:
                    lastsrc = src
                    yield ("src", src)
                continue
            m = re.match(r"\s+[0-9a-f]+:\s+\S+\s+(.*)$", line)
            if not m:
                continue
            ins = m.group(1)
            mm = re.match(r"ld\s+t9,(-?\d+)\(gp\)", ins)
            if mm:
                pend = self.name_of(int(mm.group(1)))
                continue
            # Integer literals into argument registers, so a call site shows the
            # constants the vendor passed -- an i2c address, a bus number, a bit.
            mm = re.match(r"li\s+(a[0-9]),(-?\d+)", ins)
            if mm:
                args[mm.group(1)] = int(mm.group(2))
                continue
            if "jalr" in ins and "t9" in ins:
                yield ("call", pend or "<no preceding GOT load>",
                       " ".join("%s=%#x" % (k, v) for k, v in sorted(args.items())))
                pend, args = None, {}


SELFTEST_RANGE = (0x1001D050, 0x1001D1C8)   # gryphon_read_sfp_state
SELFTEST_EXPECT = ["gryphon_sfp_i2c_map", "libi2c_open",
                   "libi2c_read_cmd_byte", "libi2c_read_cmd_byte",
                   "libi2c_close"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("elf")
    ap.add_argument("lo", nargs="?", help="start address, e.g. 0x1260ffb0")
    ap.add_argument("hi", nargs="?")
    ap.add_argument("--prefix", default=DEFAULT_PREFIX,
                    help="cross-binutils prefix")
    ap.add_argument("--selftest", action="store_true",
                    help="re-derive the known calls in gryphon_read_sfp_state; "
                         "pass it libports.so")
    args = ap.parse_args()

    r = Resolver(args.elf, args.prefix)

    if args.selftest:
        got = [c[1] for c in r.calls(*SELFTEST_RANGE) if c[0] == "call"]
        print("expected: %s" % " ".join(SELFTEST_EXPECT))
        print("got:      %s" % " ".join(got))
        if got == SELFTEST_EXPECT:
            print("PASS")
            return 0
        print("FAIL -- the GOT index mapping is wrong; every name this tool "
              "prints is suspect")
        return 1

    if not (args.lo and args.hi):
        ap.error("give lo and hi addresses, or --selftest")
    for item in r.calls(int(args.lo, 0), int(args.hi, 0)):
        if item[0] == "src":
            print("  %s" % item[1])
        else:
            print("        --> %s   [%s]" % (item[1], item[2]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
