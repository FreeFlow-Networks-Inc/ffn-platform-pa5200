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
Re-deriving the map from the library each time is better than depending on a
file nobody can regenerate: the library is on the appliance, so this works
wherever the appliance is.

Nothing here is pinned to one PAN-OS build
------------------------------------------
The previous version hardcoded DESC_VADDR/FLD_VADDR to the addresses they had
in a 9.0.x library and aborted on any count mismatch, so it could not read any
other build at all. The tables are now resolved the way the linker records
them:

  * fe100_csr_desc_db and fe100_csr_fields_db are looked up in .symtab
    (falling back to .dynsym),
  * virtual addresses become file offsets through the PT_LOAD program headers
    rather than a fixed 0x10000000 bias,
  * the record layout is detected, because it changed between builds.

Two record layouts are known, and they agree about almost nothing
-----------------------------------------------------------------
    9.0.x   28 B:  name u64 | hiername u64 | addr u32 | packed u32 | rstval u32
                   packed = field_num:6 (high) | field_off:26
    11.2.x  20 B:  name u32 | addr u32 | packed u32 | 8-byte relocated slot
                   packed = is_wide[31] | stride_off[30:25]
                          | field_num[24:19] | field_off[18:0]

In the 11.2 layout the name at +0 is an absolute vaddr stored in place, while
the trailing 8-byte slot is relocated and therefore reads as zero in the file.

Never trust a symbol size or a relocation count for a record count
------------------------------------------------------------------
In the 11.2 library fe100_csr_desc_db is declared 0x1d100 bytes -- 5952 records
of 20 -- and carries exactly 5951 R_MIPS_REL32 relocations spaced uniformly 20
bytes apart. Both look authoritative and both are wrong: the real array ends
after 2675 records, and the remainder of that symbol is an unrelated
counter-description table holding pointer pairs into strings such as "Packets
entered module flow stage fastpath". The array is therefore walked until a
record fails to resolve -- the all-zero sentinel -- and never past it.

Observed counts, both reproduced by this script:

    9.0.x       5951 registers, 3061 field records, ~23 blocks
    11.2.4-h5   2675 registers, field layout not yet recognised, 9 blocks

The 11.2 table is the smaller of the two, so a newer library is not
automatically the better source -- keep the 9.0.x-derived map authoritative and
use the 11.2 one as a cross-check on the blocks it does cover.

A count mismatch is reported, never fatal. Refusing to emit anything is what
made the previous version useless against a new build.
"""
import argparse
import collections
import json
import struct
import sys

Layout = collections.namedtuple("Layout", "name size unpack")


def _desc_11_2(blob, off, rd):
    namep, addr, packed = struct.unpack_from(rd + "3I", blob, off)
    return namep, addr, {
        "is_wide": packed >> 31,
        "stride_off": (packed >> 25) & 0x3F,
        "field_num": (packed >> 19) & 0x3F,
        "field_off": packed & 0x7FFFF,
    }


def _desc_9_0(blob, off, rd):
    namep, _hier, addr, packed, rst = struct.unpack_from(rd + "QQIII", blob, off)
    return namep, addr, {
        "is_wide": 0,
        "stride_off": 0,
        "field_num": packed >> 26,
        "field_off": packed & 0x3FFFFFF,
        "rstval": rst,
    }


DESC_LAYOUTS = [Layout("11.2.x/20B", 20, _desc_11_2),
                Layout("9.0.x/28B", 28, _desc_9_0)]


class Elf:
    """Just enough ELF to resolve a symbol and map a vaddr to a file offset."""

    def __init__(self, path):
        with open(path, "rb") as fh:
            self.blob = fh.read()
        b = self.blob
        if b[:4] != b"\x7fELF":
            raise ValueError("%s is not an ELF file" % path)
        if b[4] != 2:
            raise ValueError("only ELF64 is supported")
        self.rd = ">" if b[5] == 2 else "<"
        self.phoff, self.shoff = struct.unpack_from(self.rd + "QQ", b, 32)
        (self.phentsize, self.phnum, self.shentsize,
         self.shnum) = struct.unpack_from(self.rd + "HHHH", b, 54)
        self.loads = []
        for i in range(self.phnum):
            o = self.phoff + i * self.phentsize
            ptype, = struct.unpack_from(self.rd + "I", b, o)
            if ptype != 1:                       # PT_LOAD
                continue
            poff, pvaddr, _pp, pfilesz = struct.unpack_from(self.rd + "QQQQ", b, o + 8)
            self.loads.append((pvaddr, poff, pfilesz))
        self.maps = [(s["addr"], s["off"], s["size"]) for s in self._sections()
                     if s["type"] not in (0, 8) and s["addr"]]   # skip NULL/NOBITS
        # An 11.2 library was seen whose .data.rel.ro header places the CSR
        # tables 0x10000 below where their contents actually are, while .rodata
        # in the same file is self-consistent. Both the section header and the
        # program header agree with each other and disagree with the bytes, so
        # a table base is *validated* (see detect_desc) rather than trusted --
        # these are the deltas worth trying.
        self.deltas = sorted({a - o for a, o, _ in self.maps} |
                             {v - o for v, o, _ in self.loads})
        self.symbols = self._symbols()

    def candidates(self, vaddr):
        """Plausible file offsets for a vaddr, best guess first."""
        out, seen = [], set()
        for cand in [self.off(vaddr)] + [vaddr - d for d in self.deltas]:
            if cand is not None and 0 <= cand < len(self.blob) and cand not in seen:
                seen.add(cand)
                out.append(cand)
        return out

    def _sections(self):
        out = []
        for i in range(self.shnum):
            o = self.shoff + i * self.shentsize
            stype, = struct.unpack_from(self.rd + "I", self.blob, o + 4)
            addr, off, size = struct.unpack_from(self.rd + "QQQ", self.blob, o + 16)
            link, = struct.unpack_from(self.rd + "I", self.blob, o + 40)
            out.append({"type": stype, "addr": addr, "off": off,
                        "size": size, "link": link})
        return out

    def _symbols(self):
        secs = self._sections()
        syms = {}
        for want in (2, 11):                     # SHT_SYMTAB, then SHT_DYNSYM
            for s in secs:
                if s["type"] != want:
                    continue
                strtab = secs[s["link"]]
                for i in range(s["size"] // 24):
                    o = s["off"] + i * 24
                    st_name, = struct.unpack_from(self.rd + "I", self.blob, o)
                    st_value, st_size = struct.unpack_from(self.rd + "QQ", self.blob, o + 8)
                    if not st_name:
                        continue
                    beg = strtab["off"] + st_name
                    end = self.blob.find(b"\0", beg)
                    nm = self.blob[beg:end].decode("ascii", "replace")
                    syms.setdefault(nm, (st_value, st_size))
            if syms:
                break
        return syms

    def off(self, vaddr):
        for base, foff, size in self.maps:
            if base <= vaddr < base + size:
                return foff + (vaddr - base)
        for pvaddr, poff, pfilesz in self.loads:
            if pvaddr <= vaddr < pvaddr + pfilesz:
                return poff + (vaddr - pvaddr)
        return None

    def cstr(self, vaddr, limit=160):
        o = self.off(vaddr) if vaddr else None
        if o is None:
            return None
        end = self.blob.find(b"\0", o, o + limit)
        if end < 0:
            return None
        try:
            s = self.blob[o:end].decode("ascii")
        except UnicodeDecodeError:
            return None
        return s if s and (s[0].isalpha() or s[0] == "_") else None


def detect_desc(elf, base):
    """Find the (layout, file offset) whose leading records are real registers.

    Both the layout and the base offset are validated against the bytes: eight
    consecutive records must carry a resolvable ASCII name and an address
    inside the 1 MB BAR0. Guessing either one produces plausible-looking
    nonsense, which is exactly what this is here to prevent.
    """
    for off in elf.candidates(base):
        for lay in DESC_LAYOUTS:
            good = 0
            for i in range(8):
                if off + (i + 1) * lay.size > len(elf.blob):
                    break
                namep, addr, _ = lay.unpack(elf.blob, off + i * lay.size, elf.rd)
                if elf.cstr(namep) and addr < (1 << 20):
                    good += 1
                else:
                    break
            if good == 8:
                return lay, off
    return None, None


def walk(elf, o, size, lay):
    """Walk records until one fails to resolve; that sentinel ends the array."""
    regs = []
    slots = size // lay.size
    for i in range(slots):
        namep, addr, meta = lay.unpack(elf.blob, o + i * lay.size, elf.rd)
        nm = elf.cstr(namep)
        if nm is None:
            break
        rec = {"name": nm, "addr": addr}
        rec.update(meta)
        regs.append(rec)
    return regs, slots


def read_fields(elf, base, size):
    """Try the known field layouts; emit nothing rather than emit garbage."""
    for o in elf.candidates(base):
        for name, rec_sz in (("9.0.x/10B", 10), ("11.2.x/12B", 12)):
            n = size // rec_sz
            if o + n * rec_sz > len(elf.blob):
                continue
            out, ok = [], 0
            for i in range(n):
                p = o + i * rec_sz
                if rec_sz == 10:
                    namep, msb, lsb = struct.unpack_from(elf.rd + "QBB", elf.blob, p)
                else:
                    namep, = struct.unpack_from(elf.rd + "I", elf.blob, p)
                    msb, lsb = struct.unpack_from(elf.rd + "HH", elf.blob, p + 4)
                nm = elf.cstr(namep)
                out.append({"name": nm, "msb": msb, "lsb": lsb})
                if nm and 0 <= lsb <= msb <= 31:
                    ok += 1
            if n and ok >= 0.9 * n:
                return name, out, ok
    return None, [], 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lib", required=True, help="path to libpandp_cp.so.1.0")
    ap.add_argument("--json", help="write the whole map here")
    ap.add_argument("--grep", help="print registers whose name contains this")
    ap.add_argument("--block", help="print registers in this block prefix")
    ap.add_argument("--expect", type=int,
                    help="expected register count; warns on mismatch, never fatal")
    a = ap.parse_args()

    elf = Elf(a.lib)
    try:
        desc_v, desc_n = elf.symbols["fe100_csr_desc_db"]
        fld_v, fld_n = elf.symbols["fe100_csr_fields_db"]
    except KeyError:
        return ("fe100_csr_desc_db / fe100_csr_fields_db are not in the symbol "
                "table of %s -- is this the right library?" % a.lib)

    print("desc_db   @ 0x%x  %d bytes" % (desc_v, desc_n))
    print("fields_db @ 0x%x  %d bytes" % (fld_v, fld_n))

    lay, desc_off = detect_desc(elf, desc_v)
    if lay is None:
        return ("no known descriptor layout fits this library; add one to "
                "DESC_LAYOUTS rather than guessing at the bytes")
    regs, slots = walk(elf, desc_off, desc_n, lay)
    print("layout    : %s at file offset 0x%x" % (lay.name, desc_off))
    print("registers : %d   (the symbol spans %d slots; the rest is not this array)"
          % (len(regs), slots))

    fname, fields, fok = read_fields(elf, fld_v, fld_n)
    if fname:
        print("fields    : %d via %s (%d well-formed)" % (len(fields), fname, fok))
    else:
        print("fields    : layout not recognised for this build -- omitted")

    addrs = [r["addr"] for r in regs]
    if addrs:
        over = sum(1 for x in addrs if x >= 0x100000)
        print("addr      : 0x%06x..0x%06x  (%d outside the 1 MB BAR0%s)"
              % (min(addrs), max(addrs), over, " -- SUSPECT" if over else ""))
    if a.expect is not None and len(regs) != a.expect:
        print("WARNING: expected %d registers, got %d -- the map may have changed"
              % (a.expect, len(regs)), file=sys.stderr)

    if fields:
        for r in regs:
            r["fields"] = fields[r["field_off"]: r["field_off"] + r["field_num"]]
    if a.json:
        with open(a.json, "w") as fh:
            json.dump({"library": a.lib, "layout": lay.name, "registers": regs},
                      fh, indent=1)
        print("wrote %s" % a.json)

    sel = regs
    if a.grep:
        sel = [r for r in sel if a.grep in r["name"]]
    if a.block:
        sel = [r for r in sel if r["name"].startswith(a.block)]
    if a.grep or a.block:
        print("\n%-46s %-10s fields" % ("name", "addr"))
        print("-" * 72)
        for r in sel[:200]:
            names = ",".join(f["name"] or "?" for f in r.get("fields", []))
            print("%-46s 0x%08x  %s" % (r["name"], r["addr"], names[:40]))
        print("(%d matched)" % len(sel))
    else:
        blocks = collections.Counter(r["name"].split("_")[0] for r in regs)
        print("\nblocks by register count:")
        for b, n in blocks.most_common(40):
            print("  %-14s %4d" % (b, n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
