#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""ffn_fe100_probe.py -- read FE100 registers and compare against reset values.

Runs on the MP. Reads happen on the CP, because the FE100 sits on the CP's
PCIe bus (0002:01:00.0) and the MP cannot reach it; this drives `devmem` there
through `ffn-cpsh`.

Two things make this worth having as a tool rather than a one-off command.

**The byte swap.** The CP is MIPS64 big-endian and the CSR window is not, so
every 32-bit read comes back byte-reversed. That is not a guess: four NIF
registers were read raw and matched their documented reset values exactly after
a 32-bit swap --

    nif_rst_ctrl       0xFFFF0F00 -> 0x000FFFFF   matches
    nif_p0_mac_pcs_cfg 0x90910400 -> 0x00049190   matches
    nif_p1_mac_pcs_cfg 0x90920400 -> 0x00049290   matches
    nif_cr_imp_chk_en  0x0F000000 -> 0x0000000F   matches

Four for four is not coincidence. Every read here is swapped before it is
compared or printed.

**Batching.** Each ffn-cpsh round trip costs seconds, so all the reads for one
invocation go down as a single command line and come back together. Probing a
block one register at a time is unusably slow over the PCIe mailbox.

Reads only. Nothing here writes to the chip.
"""
import argparse
import json
import re
import subprocess
import sys

BAR0 = 0x11d00f0000000          # from /sys/bus/pci/devices/0002:01:00.0/resource
CPSH = "ffn-cpsh"
DEFAULT_MAP = "/root/fe100-csr.json"


def swap32(v):
    """The CSR window is opposite-endian to the CP. See the module docstring."""
    return ((v & 0x000000FF) << 24 | (v & 0x0000FF00) << 8 |
            (v & 0x00FF0000) >> 8  | (v & 0xFF000000) >> 24)


def load_map(path):
    with open(path) as fh:
        regs = json.load(fh)
    return {r["name"]: r for r in regs}


def read_regs(names, regmap, timeout=180):
    """One ffn-cpsh round trip for the whole batch."""
    cmds = []
    for n in names:
        addr = BAR0 + regmap[n]["addr"]
        cmds.append("devmem 0x%x 32" % addr)
    script = "; ".join(cmds)

    out = subprocess.run(
        [CPSH, "-c", script],
        capture_output=True, text=True, timeout=timeout).stdout

    vals = [int(m, 16) for m in re.findall(r"0x([0-9A-Fa-f]{8})\b", out)]
    if len(vals) != len(names):
        sys.stderr.write("warning: asked for %d registers, parsed %d values\n"
                         % (len(names), len(vals)))
    return vals


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--map", default=DEFAULT_MAP)
    ap.add_argument("--block", help="probe every register whose name starts with this")
    ap.add_argument("--regs", nargs="*", help="probe these registers by name")
    ap.add_argument("--limit", type=int, default=48,
                    help="cap the batch; the mailbox is slow and a whole block is large")
    ap.add_argument("--changed", action="store_true",
                    help="only show registers that differ from their reset value")
    a = ap.parse_args()

    regmap = load_map(a.map)

    if a.regs:
        names = [n for n in a.regs if n in regmap]
        missing = [n for n in a.regs if n not in regmap]
        for n in missing:
            sys.stderr.write("unknown register: %s\n" % n)
    elif a.block:
        names = sorted((n for n in regmap if n.startswith(a.block)),
                       key=lambda n: regmap[n]["addr"])
    else:
        sys.stderr.write("need --block or --regs\n")
        return 2

    names = names[:a.limit]
    if not names:
        sys.stderr.write("nothing to probe\n")
        return 2

    vals = read_regs(names, regmap)

    print("%-34s %-10s %-11s %-11s" % ("register", "addr", "read", "reset"))
    print("-" * 74)
    ndiff = 0
    for n, raw in zip(names, vals):
        r = regmap[n]
        got = swap32(raw)
        same = (got == r["rstval"])
        if not same:
            ndiff += 1
        if a.changed and same:
            continue
        # All-ones after the swap means the read did not complete. It is not a
        # value, and reporting it as one has misled before.
        note = "  <-- READ FAILED" if raw == 0xFFFFFFFF else ("" if same else "  <-- differs")
        print("%-34s 0x%08x 0x%08x  0x%08x%s"
              % (n, r["addr"], got, r["rstval"], note))

    print("\n%d/%d registers differ from reset" % (ndiff, len(names)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
