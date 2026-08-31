#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""ffn_bcmregs.py -- recover the BCM88375 register field database.

FFN cannot program the switch without knowing what each register's bits mean,
and guessing bit positions means writing guesses to live silicon. The vendor's
SDK application ships with full debug info and carries the register field
database as ordinary initialised data, so the meanings are recoverable exactly.

This tool reads that data and emits JSON. It is FFN's own code; the data it
reads is the appliance owner's vendor firmware, used in place. The OUTPUT is
derived vendor data and must be kept out of FFN's shipped images -- write it
somewhere local, not into a repository or an image tree. ffn_vendor.py's
check-clean gate exists for this reason.

## Why not gdb, readelf or pyelftools

gdb is not installed on the analysis VM, so `ptype` is unavailable. readelf and
nm are available but a 487 MB object with a 7.5 MB symbol table makes repeated
subprocess parsing slow and fragile, and pyelftools is not installed either. So
this parses ELF64 directly. It is about 100 lines and removes every external
dependency.

## The data layout, which is established rather than assumed

Each field array is a `soc_field_info_t[]`, 12 bytes per entry, big-endian:

    struct { uint32 field_id; uint16 len; uint16 bp; uint32 flags; }

Confirmed by size/count agreement across many registers (84/7, 36/3, 24/2,
12/1) and by the decoded values matching registers whose behaviour is already
known on hardware. Entries are sorted by `field_id`, which is the SDK's own
alphabetical field-name ordering -- NOT by bit position, so sort before display.
`flags & 0x08000000` marks a read-only field.

Names are not in the array. `soc_fieldnames` is an array of pointers, 8 bytes
each big-endian, indexed by `field_id`. Names like `FIELD_1_3` are the SDK's own
placeholders for fields it does not document, not extraction failures.

## Verification, not trust

The recovered map is checked against registers FFN has already read on live
silicon through its own driver: CMIC_LEDUP0_CTRL's LEDUP_EN at bit 0 (setting it
demonstrably starts the LED processor) and CMIC_COMMON_SCHAN_CTRL's MSG_START /
MSG_DONE / ABORT and the four read-only error bits. `verify` re-checks those and
fails loudly on a mismatch, because a silently wrong field map is worse than
none.

## Silicon revision: checked, and it does not matter

The object carries both BCM88375_A0 (14,314 field arrays) and BCM88375_B0
(14,336). The appliance reports PCI revision 0x11, which by Broadcom's usual
convention is B0 rather than A0, so this looked like it mattered.

It does not. Of 14,303 registers present in both revisions, **14,302 are
identical** and the single difference is `IHP_RESERVED_SPARE_2` -- a reserved
spare, where A0 declares one 32-bit field and B0 splits it into 1 + 31 bits.
Every register FFN actually touches (SCHAN_CTRL, LEDUP0_CTRL, FSCHAN_OPCODE,
FSCHAN_STATUS, MIIM_CTRL) is byte-identical between them. 33 registers exist
only in B0 and 11 only in A0.

So the A0 default is safe. Use --chip to switch if a specific register ever
turns out to be revision sensitive; do not re-derive this comparison.

## What the field database cannot tell you

Worth knowing before hoping it solves the S-channel problem: FSCHAN_OPCODE is
declared as ONE opaque 32-bit field (named `ADDRRESS`, the vendor's own typo)
and SCHAN_MESSAGE0 as one opaque 32-bit `DATA_N`. FSCHAN_STATUS is a single
read-only `FSCHAN_BUSY[0]`.

That settles the transaction MECHANICS -- write the word, poll BUSY, read
DATA32 -- but the SBUS command word's internal encoding is not in any register
descriptor, because the hardware does not decompose it. That encoding lives in
the SDK's address-construction code and has to come from there.

Commands:
    symbols  --elf F              what field arrays exist, and for which chips
    extract  --elf F [--out J]    recover the field database
    show     --elf F --reg NAME   one register's fields, decoded
    verify   --elf F              check against what hardware has confirmed
    selftest                      parser self-checks, no vendor data needed
"""

import argparse
import json
import os
import re
import struct
import sys

DEFAULT_CHIP = "BCM88375_A0"

# Field arrays are named soc_<REGISTER>_<CHIP>r_fields in the symbol table.
FIELDS_SUFFIX_RE = re.compile(r"^soc_(?P<reg>.+)_(?P<chip>BCM\w+?)r_fields$")

FIELD_ENTRY_SIZE = 12
FLAG_READ_ONLY = 0x08000000

# Registers whose bit meanings are already confirmed by FFN's own driver against
# live silicon. If the extraction disagrees with these, the extraction is wrong.
KNOWN_GOOD = {
    "CMIC_LEDUP0_CTRL": {"LEDUP_EN": (0, 1)},
    "CMIC_COMMON_SCHAN_CTRL": {
        "MSG_START": (0, 1),
        "MSG_DONE": (1, 1),
        "ABORT": (2, 1),
        "SER_CHECK_FAIL": (20, 1),
        "NACK": (21, 1),
        "TIMEOUT": (22, 1),
        "SCHAN_ERROR": (23, 1),
    },
}
KNOWN_READ_ONLY = {
    ("CMIC_COMMON_SCHAN_CTRL", "SER_CHECK_FAIL"),
    ("CMIC_COMMON_SCHAN_CTRL", "NACK"),
    ("CMIC_COMMON_SCHAN_CTRL", "TIMEOUT"),
    ("CMIC_COMMON_SCHAN_CTRL", "SCHAN_ERROR"),
}


# --------------------------------------------------------------- ELF reader ---
class Elf:
    """Minimal ELF64 reader. Big- and little-endian, symbols and vaddr mapping.

    Deliberately not a general ELF library: it does the four things needed here
    and nothing else, so there is very little to be wrong.
    """

    def __init__(self, path):
        self.path = path
        self.f = open(path, "rb")
        ident = self.f.read(16)
        if ident[:4] != b"\x7fELF":
            raise ValueError("%s is not an ELF file" % path)
        if ident[4] != 2:
            raise ValueError("%s is not ELF64 (this tool needs the 64-bit object)"
                             % path)
        self.big = ident[5] == 2
        self.e = ">" if self.big else "<"
        self._sections = None
        self._symbols = None

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass

    def _u(self, fmt, off, size):
        self.f.seek(off)
        b = self.f.read(size)
        if len(b) != size:
            raise ValueError("short read at 0x%x" % off)
        return struct.unpack(self.e + fmt, b)

    @property
    def sections(self):
        if self._sections is not None:
            return self._sections
        # ELF64 header: e_shoff at 0x28, e_shentsize 0x3a, e_shnum 0x3c,
        # e_shstrndx 0x3e
        (shoff,) = self._u("Q", 0x28, 8)
        shentsize, shnum, shstrndx = self._u("HHH", 0x3A, 6)
        raw = []
        for i in range(shnum):
            base = shoff + i * shentsize
            (name, typ, flags, addr, offset, size, link, info, align,
             entsize) = self._u("IIQQQQIIQQ", base, 64)
            raw.append({"name_off": name, "type": typ, "flags": flags,
                        "addr": addr, "offset": offset, "size": size,
                        "link": link, "info": info, "entsize": entsize})
        # Resolve names from the section header string table.
        shstr = raw[shstrndx]
        self.f.seek(shstr["offset"])
        strtab = self.f.read(shstr["size"])
        for s in raw:
            end = strtab.find(b"\0", s["name_off"])
            s["name"] = strtab[s["name_off"]:end].decode("utf-8", "replace")
        self._sections = raw
        return raw

    def section(self, name):
        for s in self.sections:
            if s["name"] == name:
                return s
        return None

    def vaddr_to_offset(self, vaddr, need=1):
        """Map a virtual address to a file offset.

        SHT_NOBITS (.bss) occupies address space but no file bytes, so it is
        excluded: a hit there means the data is zero at rest and there is
        nothing to read.
        """
        SHT_NOBITS = 8
        for s in self.sections:
            if s["type"] == SHT_NOBITS or not s["addr"]:
                continue
            if s["addr"] <= vaddr and vaddr + need <= s["addr"] + s["size"]:
                return s["offset"] + (vaddr - s["addr"]), s["name"]
        return None, None

    def read_at_vaddr(self, vaddr, size):
        off, _sec = self.vaddr_to_offset(vaddr, size)
        if off is None:
            return None
        self.f.seek(off)
        return self.f.read(size)

    @property
    def symbols(self):
        """{name: (value, size)} from .symtab, falling back to .dynsym."""
        if self._symbols is not None:
            return self._symbols
        out = {}
        for symname in (".symtab", ".dynsym"):
            sec = self.section(symname)
            if not sec or not sec["size"]:
                continue
            strsec = self.sections[sec["link"]]
            self.f.seek(strsec["offset"])
            strtab = self.f.read(strsec["size"])
            entsize = sec["entsize"] or 24
            count = sec["size"] // entsize
            self.f.seek(sec["offset"])
            blob = self.f.read(sec["size"])
            # Elf64_Sym: name(4) info(1) other(1) shndx(2) value(8) size(8)
            unpack = struct.Struct(self.e + "IBBHQQ").unpack_from
            for i in range(count):
                try:
                    nameoff, _info, _other, _shndx, value, size = unpack(
                        blob, i * entsize)
                except struct.error:
                    continue
                if not nameoff:
                    continue
                end = strtab.find(b"\0", nameoff)
                if end < 0:
                    continue
                nm = strtab[nameoff:end].decode("utf-8", "replace")
                if nm and nm not in out:
                    out[nm] = (value, size)
            if out:
                break
        self._symbols = out
        return out


# ------------------------------------------------------------- field decode ---
def parse_field_array(blob):
    """Decode a soc_field_info_t[] blob. Returns a list of raw field records."""
    out = []
    n = len(blob) // FIELD_ENTRY_SIZE
    unpack = struct.Struct(">IHHI").unpack_from
    for i in range(n):
        fid, flen, bp, flags = unpack(blob, i * FIELD_ENTRY_SIZE)
        out.append({"field_id": fid, "len": flen, "bp": bp, "flags": flags,
                    "ro": bool(flags & FLAG_READ_ONLY)})
    return out


class NameTable:
    """soc_fieldnames: an array of pointers indexed by field_id."""

    def __init__(self, elf, sym="soc_fieldnames"):
        self.elf = elf
        self.base = None
        self.count = 0
        self._cache = {}
        s = elf.symbols.get(sym)
        if not s:
            return
        vaddr, size = s
        self.base = vaddr
        self.count = size // 8 if size else 0

    @property
    def ok(self):
        return self.base is not None

    def name(self, field_id):
        if not self.ok:
            return None
        if field_id in self._cache:
            return self._cache[field_id]
        if self.count and field_id >= self.count:
            return None
        raw = self.elf.read_at_vaddr(self.base + field_id * 8, 8)
        if not raw or len(raw) != 8:
            return None
        (ptr,) = struct.unpack(">Q", raw)
        nm = None
        if ptr:
            off, _sec = self.elf.vaddr_to_offset(ptr, 1)
            if off is not None:
                self.elf.f.seek(off)
                chunk = self.elf.f.read(128)
                end = chunk.find(b"\0")
                if end > 0:
                    nm = chunk[:end].decode("utf-8", "replace")
        self._cache[field_id] = nm
        return nm


def find_field_arrays(elf, chip=DEFAULT_CHIP):
    """{register_name: (vaddr, size)} for one chip revision."""
    out = {}
    for nm, (vaddr, size) in elf.symbols.items():
        m = FIELDS_SUFFIX_RE.match(nm)
        if not m:
            continue
        if chip and m.group("chip") != chip:
            continue
        if not size or size % FIELD_ENTRY_SIZE:
            # A field array that is not a whole number of entries is not one.
            continue
        out[m.group("reg")] = (vaddr, size)
    return out


def chip_inventory(elf):
    counts = {}
    for nm, (_v, size) in elf.symbols.items():
        m = FIELDS_SUFFIX_RE.match(nm)
        if m:
            counts[m.group("chip")] = counts.get(m.group("chip"), 0) + 1
    return counts


def extract(elf, chip=DEFAULT_CHIP, only=None):
    """Recover the field database. Returns {reg: [field, ...]}."""
    names = NameTable(elf)
    arrays = find_field_arrays(elf, chip)
    if only:
        arrays = {k: v for k, v in arrays.items() if k in only}
    out = {}
    for reg, (vaddr, size) in sorted(arrays.items()):
        blob = elf.read_at_vaddr(vaddr, size)
        if blob is None or len(blob) != size:
            continue
        fields = []
        for f in parse_field_array(blob):
            nm = names.name(f["field_id"]) if names.ok else None
            fields.append({"name": nm or ("field_id_%d" % f["field_id"]),
                           "bp": f["bp"], "len": f["len"], "ro": f["ro"],
                           "field_id": f["field_id"],
                           "named": nm is not None})
        # The SDK stores these sorted by field_id, i.e. alphabetically by name.
        # Bit order is what a human wants to read.
        fields.sort(key=lambda x: (x["bp"], x["len"]))
        out[reg] = fields
    return out


def fmt_bits(f):
    if f["len"] <= 1:
        return "[%d]" % f["bp"]
    return "[%d:%d]" % (f["bp"] + f["len"] - 1, f["bp"])


# ------------------------------------------------------------------ verify ---
def verify(db):
    """Check the extraction against what hardware has already confirmed."""
    problems = []
    checked = 0
    for reg, want in KNOWN_GOOD.items():
        got = db.get(reg)
        if not got:
            problems.append("%s: absent from the extraction" % reg)
            continue
        byname = {f["name"]: f for f in got}
        for fname, (bp, flen) in want.items():
            f = byname.get(fname)
            if not f:
                problems.append("%s.%s: not found (have: %s)"
                                % (reg, fname,
                                   ", ".join(sorted(byname)[:8]) or "nothing"))
                continue
            checked += 1
            if f["bp"] != bp or f["len"] != flen:
                problems.append("%s.%s: extracted %s, hardware-confirmed [%d:%d]"
                                % (reg, fname, fmt_bits(f), bp + flen - 1, bp))
            ro_expected = (reg, fname) in KNOWN_READ_ONLY
            if f["ro"] != ro_expected:
                problems.append("%s.%s: read-only flag is %s, expected %s"
                                % (reg, fname, f["ro"], ro_expected))
    return checked, problems


# ---------------------------------------------------------------- selftest ---
def selftest():
    fails = []
    groups = 0

    def grp(name, fn):
        nonlocal groups
        groups += 1
        try:
            fn()
        except AssertionError as e:
            fails.append("%s: %s" % (name, e))
        except Exception as e:
            fails.append("%s: unexpected %s: %s" % (name, type(e).__name__, e))

    # [1] the 12-byte big-endian entry layout round-trips
    def t1():
        blob = (struct.pack(">IHHI", 7, 1, 0, 0)
                + struct.pack(">IHHI", 9, 6, 4, FLAG_READ_ONLY))
        fs = parse_field_array(blob)
        assert len(fs) == 2, len(fs)
        assert fs[0] == {"field_id": 7, "len": 1, "bp": 0, "flags": 0,
                         "ro": False}, fs[0]
        assert fs[1]["bp"] == 4 and fs[1]["len"] == 6 and fs[1]["ro"] is True, fs[1]
    grp("[1] entry layout", t1)

    # [2] a truncated blob yields whole entries only, never a partial one
    def t2():
        blob = struct.pack(">IHHI", 3, 2, 8, 0) + b"\x00\x00\x00"
        fs = parse_field_array(blob)
        assert len(fs) == 1, "partial entry decoded: %d" % len(fs)
    grp("[2] truncation", t2)

    # [3] bit formatting matches how the SDK and datasheets read
    def t3():
        assert fmt_bits({"bp": 0, "len": 1}) == "[0]"
        assert fmt_bits({"bp": 23, "len": 1}) == "[23]"
        assert fmt_bits({"bp": 4, "len": 6}) == "[9:4]"
        assert fmt_bits({"bp": 1, "len": 3}) == "[3:1]"
    grp("[3] bit formatting", t3)

    # [4] symbol name matching, including the chip filter and the traps
    def t4():
        m = FIELDS_SUFFIX_RE.match("soc_CMIC_LEDUP0_CTRL_BCM88375_A0r_fields")
        assert m and m.group("reg") == "CMIC_LEDUP0_CTRL", m and m.group("reg")
        assert m.group("chip") == "BCM88375_A0", m.group("chip")
        # a different revision must not be mistaken for ours
        m2 = FIELDS_SUFFIX_RE.match("soc_CMIC_LEDUP0_CTRL_BCM88650_B1r_fields")
        assert m2 and m2.group("chip") == "BCM88650_B1"
        # the register descriptor, not the field array, must not match
        assert FIELDS_SUFFIX_RE.match("soc_CMIC_LEDUP0_CTRL_BCM88375_A0r") is None
        # an underscore-heavy register name must survive intact
        m3 = FIELDS_SUFFIX_RE.match(
            "soc_CMIC_CMC0_FSCHAN_OPCODE_BCM88375_A0r_fields")
        assert m3.group("reg") == "CMIC_CMC0_FSCHAN_OPCODE", m3.group("reg")
    grp("[4] symbol matching", t4)

    # [5] verify() catches a wrong bit position rather than passing it
    def t5():
        good = {"CMIC_LEDUP0_CTRL": [
                    {"name": "LEDUP_EN", "bp": 0, "len": 1, "ro": False}],
                "CMIC_COMMON_SCHAN_CTRL": [
                    {"name": n, "bp": b, "len": 1,
                     "ro": ("CMIC_COMMON_SCHAN_CTRL", n) in KNOWN_READ_ONLY}
                    for n, b in (("MSG_START", 0), ("MSG_DONE", 1),
                                 ("ABORT", 2), ("SER_CHECK_FAIL", 20),
                                 ("NACK", 21), ("TIMEOUT", 22),
                                 ("SCHAN_ERROR", 23))]}
        checked, probs = verify(good)
        assert not probs, probs
        assert checked == 8, checked
        bad = json.loads(json.dumps(good))
        bad["CMIC_LEDUP0_CTRL"][0]["bp"] = 3
        _c, probs = verify(bad)
        assert probs and "LEDUP_EN" in probs[0], probs
        # and a wrong read-only flag is caught too
        bad2 = json.loads(json.dumps(good))
        for f in bad2["CMIC_COMMON_SCHAN_CTRL"]:
            if f["name"] == "NACK":
                f["ro"] = False
        _c, probs = verify(bad2)
        assert any("read-only" in p for p in probs), probs
    grp("[5] verify catches errors", t5)

    # [6] the ELF reader works, using this Python's own interpreter binary if it
    #     is an ELF, else a synthesised minimal ELF64
    def t6():
        import tempfile
        # Synthesise a big-endian ELF64 with one section so vaddr mapping and
        # the endianness check are exercised deterministically.
        shoff = 0x40
        hdr = bytearray(0x40)
        hdr[0:4] = b"\x7fELF"
        hdr[4] = 2          # ELF64
        hdr[5] = 2          # big-endian
        hdr[6] = 1
        struct.pack_into(">Q", hdr, 0x28, shoff)
        struct.pack_into(">HHH", hdr, 0x3A, 64, 2, 1)
        # section 0: null; section 1: .shstrtab
        names = b"\0.shstrtab\0"
        s0 = bytearray(64)
        s1 = bytearray(64)
        struct.pack_into(">IIQQQQIIQQ", s1, 0, 1, 3, 0, 0x1000,
                         shoff + 128, len(names), 0, 0, 1, 0)
        blob = bytes(hdr) + bytes(s0) + bytes(s1) + names
        p = os.path.join(tempfile.mkdtemp(prefix="ffn-bcmregs-test."), "t.elf")
        with open(p, "wb") as f:
            f.write(blob)
        e = Elf(p)
        try:
            assert e.big is True, "endianness misread"
            assert e.section(".shstrtab") is not None, "section name unresolved"
            off, sec = e.vaddr_to_offset(0x1000, 1)
            assert sec == ".shstrtab" and off == shoff + 128, (off, sec)
            assert e.vaddr_to_offset(0x99999, 1)[0] is None, "bogus vaddr mapped"
        finally:
            e.close()
    grp("[6] ELF reader", t6)

    print("ffn_bcmregs selftest: %d groups, %d failed" % (groups, len(fails)))
    for f in fails:
        print("  FAIL " + f)
    return 1 if fails else 0


# --------------------------------------------------------------------- CLI ---
def main():
    ap = argparse.ArgumentParser(
        description="Recover the BCM88375 register field database from vendor "
                    "debug info (read-only; output is derived vendor data and "
                    "must not be shipped)")
    sub = ap.add_subparsers(dest="cmd")

    def add_elf(p):
        p.add_argument("--elf", required=True,
                       help="vendor debug object, e.g. .../cp/bcm.user.dbg")
        p.add_argument("--chip", default=DEFAULT_CHIP)

    add_elf(sub.add_parser("symbols", help="what field arrays exist"))
    p = sub.add_parser("extract", help="recover the field database")
    add_elf(p)
    p.add_argument("--out", default=None, help="write JSON here")
    p = sub.add_parser("show", help="one register's fields, decoded")
    add_elf(p)
    p.add_argument("--reg", required=True)
    add_elf(sub.add_parser("verify", help="check against hardware-confirmed bits"))
    sub.add_parser("selftest", help="parser self-checks, no vendor data needed")

    a = ap.parse_args()

    if a.cmd == "selftest":
        return selftest()
    if not a.cmd:
        ap.print_help()
        return 2

    if not os.path.isfile(a.elf):
        print("no such file: %s" % a.elf, file=sys.stderr)
        return 2
    elf = Elf(a.elf)
    try:
        if a.cmd == "symbols":
            inv = chip_inventory(elf)
            print("field arrays by chip revision (%d symbols total):"
                  % sum(inv.values()))
            for chip, n in sorted(inv.items(), key=lambda x: -x[1]):
                mark = "  <-- this part" if chip == a.chip else ""
                print("  %-18s %7d%s" % (chip, n, mark))
            names = NameTable(elf)
            print("soc_fieldnames: %s"
                  % ("%d entries at 0x%x" % (names.count, names.base)
                     if names.ok else "NOT FOUND -- names unavailable"))
            return 0

        if a.cmd == "show":
            db = extract(elf, a.chip, only={a.reg})
            f = db.get(a.reg)
            if not f:
                print("%s: no field array for chip %s" % (a.reg, a.chip))
                return 1
            print("%s (%s), %d fields:" % (a.reg, a.chip, len(f)))
            for x in f:
                print("  %-10s %-3s %s%s" % (fmt_bits(x),
                                             "RO" if x["ro"] else "rw",
                                             x["name"],
                                             "" if x["named"] else "  (unnamed)"))
            return 0

        if a.cmd == "verify":
            db = extract(elf, a.chip, only=set(KNOWN_GOOD))
            checked, probs = verify(db)
            for reg in sorted(KNOWN_GOOD):
                if reg in db:
                    print("%s:" % reg)
                    for x in db[reg]:
                        print("  %-10s %-3s %s" % (fmt_bits(x),
                                                   "RO" if x["ro"] else "rw",
                                                   x["name"]))
            print()
            if probs:
                print("VERIFY FAILED -- %d problem(s):" % len(probs))
                for p in probs:
                    print("  " + p)
                return 1
            print("verify OK: %d field positions match what hardware confirmed"
                  % checked)
            return 0

        if a.cmd == "extract":
            db = extract(elf, a.chip)
            nfields = sum(len(v) for v in db.values())
            named = sum(1 for v in db.values() for f in v if f["named"])
            checked, probs = verify({k: v for k, v in db.items()
                                     if k in KNOWN_GOOD})
            print("chip           %s" % a.chip)
            print("registers      %d" % len(db))
            print("fields         %d (%d named, %d placeholder)"
                  % (nfields, named, nfields - named))
            print("cross-check    %d hardware-confirmed positions, %d problem(s)"
                  % (checked, len(probs)))
            for p in probs:
                print("    " + p)
            if a.out:
                doc = {"source": os.path.abspath(a.elf), "chip": a.chip,
                       "entry_size": FIELD_ENTRY_SIZE, "byte_order": "big",
                       "read_only_flag": FLAG_READ_ONLY,
                       "registers": db}
                with open(a.out, "w") as fh:
                    json.dump(doc, fh, indent=1, sort_keys=True)
                print("wrote          %s (%d bytes)"
                      % (a.out, os.path.getsize(a.out)))
                print()
                print("This output is derived vendor data. Keep it local: it "
                      "must not enter an FFN repository or image.")
            return 1 if probs else 0
    finally:
        elf.close()

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
