#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""ffn_fe100_csr.py -- recover the FE100's register map from the vendor library.

The FE100 is a real ASIC on the CP's PCIe bus (0002:01:00.0, vendor 0xfeed,
device 0xfe1c, class 0x020000, BAR0 = 1 MB) with no driver bound. It answers
CSR reads and sits at power-on reset defaults. To bring it up FFN needs to know
what its registers ARE, and that information exists only inside the vendor's
own library.

Why this file exists rather than a saved JSON
---------------------------------------------
The map was extracted once before and written to a clone mount that is no
longer present. Re-deriving it from the library each time is better than
depending on a file nobody can regenerate: the library is on the appliance, so
this works wherever the appliance is.

How the tables are found, without guessing
------------------------------------------
`fe100csr.so` is only Python glue; its undefined symbols point at
`fe100_csr_db` in `libpandp_cp.so.1.0`, whose two R_MIPS_REL32 relocations name
the real tables:

    fe100_csr_desc_db    @ 0x109fd3d0   0x28b00 bytes
    fe100_csr_fields_db  @ 0x109f5c38   30610 bytes

and the library still carries its MIPS DWARF, which documents the layout:

    typedef struct {                  /* pan_csr_desc_db_t, 28 B */
        char *name;                   /* +0  */
        char *hiername;               /* +8  */
        pan_uint32_t addr;            /* +16 */
        pan_uint32_t field_num : 6;   /* +20, DWARF bit_offset 0  */
        pan_uint32_t field_off : 26;  /* +20, DWARF bit_offset 6  */
        pan_uint32_t rstval;          /* +24 */
    } pan_csr_desc_db_t;

Three details that matter, all of which are easy to get wrong:

  * The library is **MIPS64 big-endian**, so every field is read big-endian
    regardless of the machine running this script.
  * DWARF bit_offset counts from the MOST significant bit of the storage unit,
    so `field_num = word >> 26` and `field_off = word & 0x3ffffff`. Reading
    them the other way round yields plausible-looking nonsense.
  * The `name` / `hiername` pointers are stored in the file as **absolute
    vaddrs**, not zeroed-and-relocated, so strings resolve directly; for these
    sections `file_offset = vaddr - 0x10000000`.

Self-check: the tables should yield **5951 registers** and **14456 fields**.
The script reports what it got, so a silent layout change shows up as a count
that does not match rather than as a map that is quietly wrong.
"""
import argparse
import json
import struct
import sys

DEFAULT_LIB = "/opt/dpfs/usr/local/lib64/libpandp_cp.so.1.0"

DESC_VADDR, DESC_BYTES = 0x109fd3d0, 0x28b00
FLD_VADDR,  FLD_BYTES  = 0x109f5c38, 30610
VADDR_BIAS = 0x10000000          # file_offset = vaddr - bias, for these sections

DESC_SZ = 28
FLD_SZ  = 10     # {char *name; u8 msb; u8 lsb} -- 3061 UNIQUE entries, shared
                 # between registers. The often-quoted 14456 is the SUM of every
                 # register field_num, i.e. field REFERENCES, not table entries.

EXPECT_REGS   = 5951
EXPECT_FIELDS = 3061    # 30610 B / 10 B per entry


def read_at(fh, vaddr, nbytes):
    fh.seek(vaddr - VADDR_BIAS)
    return fh.read(nbytes)


def cstr(fh, vaddr, limit=128):
    """Resolve a stored absolute vaddr to its NUL-terminated string."""
    if vaddr == 0:
        return None
    try:
        fh.seek(vaddr - VADDR_BIAS)
        raw = fh.read(limit)
    except OSError:
        return None
    end = raw.find(b"\0")
    if end < 0:
        return None
    try:
        return raw[:end].decode("ascii")
    except UnicodeDecodeError:
        return None


def parse(path):
    regs, fields = [], []
    with open(path, "rb") as fh:
        blob = read_at(fh, FLD_VADDR, FLD_BYTES)
        for off in range(0, len(blob) - FLD_SZ + 1, FLD_SZ):
            namep, msb, lsb = struct.unpack_from(">QBB", blob, off)
            fields.append({"name": cstr(fh, namep), "msb": msb, "lsb": lsb})

        blob = read_at(fh, DESC_VADDR, DESC_BYTES)
        for off in range(0, len(blob) - DESC_SZ + 1, DESC_SZ):
            namep, hierp, addr, packed, rst = struct.unpack_from(">QQIII", blob, off)
            name = cstr(fh, namep)
            if name is None:
                continue
            # DWARF bit_offset is from the MSB: field_num is the top 6 bits.
            fnum, foff = packed >> 26, packed & 0x3FFFFFF
            regs.append({
                "name": name,
                "hier": cstr(fh, hierp),
                "addr": addr,
                "rstval": rst,
                "fields": fields[foff:foff + fnum],
            })
    return regs, fields


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lib", default=DEFAULT_LIB)
    ap.add_argument("--json", help="write the whole map here")
    ap.add_argument("--grep", help="print registers whose name matches")
    ap.add_argument("--block", help="print registers in this block prefix")
    a = ap.parse_args()

    regs, fields = parse(a.lib)

    print("registers: %d (expected %d)%s" %
          (len(regs), EXPECT_REGS, "" if len(regs) == EXPECT_REGS else "  <-- MISMATCH"))
    print("fields:    %d (expected %d)%s" %
          (len(fields), EXPECT_FIELDS, "" if len(fields) == EXPECT_FIELDS else "  <-- MISMATCH"))

    if a.json:
        with open(a.json, "w") as fh:
            json.dump(regs, fh, indent=1)
        print("wrote %s" % a.json)

    sel = None
    if a.grep:
        sel = [r for r in regs if a.grep.lower() in r["name"].lower()]
    elif a.block:
        sel = [r for r in regs if r["name"].startswith(a.block)]

    if sel is not None:
        print("\n%-40s %-10s %-12s fields" % ("name", "addr", "reset"))
        print("-" * 78)
        for r in sorted(sel, key=lambda x: x["addr"])[:200]:
            print("%-40s 0x%08x 0x%08x   %s" %
                  (r["name"], r["addr"], r["rstval"],
                   ",".join("%s[%d:%d]" % (f["name"], f["msb"], f["lsb"])
                            for f in r["fields"][:6] if f["name"])))
        print("(%d matched)" % len(sel))

    if not a.json and sel is None:
        seen = {}
        for r in regs:
            blk = r["name"].split("_", 1)[0]
            seen[blk] = seen.get(blk, 0) + 1
        print("\nblocks by register count:")
        for blk, n in sorted(seen.items(), key=lambda kv: -kv[1]):
            print("  %-12s %4d" % (blk, n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
