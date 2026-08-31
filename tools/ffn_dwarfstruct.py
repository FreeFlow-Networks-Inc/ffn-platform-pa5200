#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""ffn_dwarfstruct.py -- recover a C struct's exact layout from DWARF.

Written to answer one blocking question -- what does the BCM88375's S-channel
command word actually look like -- but kept general, because the same need keeps
recurring: a vendor binary carries the precise layout of a hardware-facing
struct, and FFN needs it exactly rather than approximately.

## Why this exists at all

The S-channel register descriptors are useless for this. `CMIC_FSCHAN_OPCODE`
declares a single opaque 32-bit field and `CMIC_SCHAN_MESSAGE0` a single
`DATA_N`, because the hardware does not decompose those words -- software builds
them. So the encoding is not in any register database; it is in the layout of
the C structs the SDK uses to build the message, and those are in DWARF:

    schan_msg_t             the union
    schan_msg_readcmd_t     a register READ command
    schan_msg_readresp_t    its response
    schan_msg_writecmd_t    a register WRITE command
    schan_msg_plain_t       raw words

Guessing a bit layout means writing guesses to a live switch. Reading it out of
the vendor's own type information means not guessing.

## Bitfield bit numbering, which is the easy thing to get wrong

DWARF 2 and 3 describe a bitfield with three attributes: the containing storage
unit's size (`DW_AT_byte_size`), and the field's `DW_AT_bit_size` plus
`DW_AT_bit_offset`. Per the DWARF spec, `bit_offset` counts from the **high-order
bit** of that storage unit, regardless of target endianness. So the position of
the field's least-significant bit within the unit is:

    lsb = byte_size * 8 - bit_offset - bit_size

DWARF 4 replaced this with `DW_AT_data_bit_offset`, measured in bits from the
start of the containing struct and NOT endianness-relative, which needs no
conversion.

This tool reports the derived `[msb:lsb]` AND the raw attributes it derived them
from, so the arithmetic is auditable rather than trusted. When a struct's total
size does not match the sum of its fields, it says so instead of quietly
presenting a layout with a hole in it.

## Performance, on a 238 MB .debug_info

Walking every DIE in a quarter-gigabyte of DWARF in Python is not viable, and is
not necessary. Instead:

  1. find the type name in `.debug_str`, giving a 4-byte `DW_FORM_strp` value;
  2. build a CU index by hopping `unit_length` from header to header, which
     touches only a few bytes per CU;
  3. chunk-scan `.debug_info` for that strp value, and separately for the
     inline `DW_FORM_string` bytes, to get candidate offsets;
  4. fully parse only the CUs that actually contain a candidate.

Parsing one CU is cheap, so the expensive part is a byte scan rather than a DIE
walk.

Commands:
    find    --elf F --name N        which CUs define this type
    layout  --elf F --name N        the struct layout, decoded
    types   --elf F --grep PAT      type names matching a pattern
    selftest                        self-checks, no vendor data needed
"""

import argparse
import json
import os
import re
import struct
import sys

# ------------------------------------------------------------------- DWARF ---
DW_TAG_structure_type = 0x13
DW_TAG_union_type = 0x17
DW_TAG_typedef = 0x16
DW_TAG_member = 0x0D
DW_TAG_base_type = 0x24
DW_TAG_enumeration_type = 0x04
DW_TAG_pointer_type = 0x0F
DW_TAG_const_type = 0x26
DW_TAG_volatile_type = 0x35
DW_TAG_array_type = 0x01

DW_AT_name = 0x03
DW_AT_byte_size = 0x0B
DW_AT_bit_offset = 0x0C
DW_AT_bit_size = 0x0D
DW_AT_declaration = 0x3C
DW_AT_encoding = 0x3E
DW_AT_data_member_location = 0x38
DW_AT_type = 0x49
DW_AT_data_bit_offset = 0x6B

TAG_NAMES = {
    DW_TAG_structure_type: "struct",
    DW_TAG_union_type: "union",
    DW_TAG_typedef: "typedef",
    DW_TAG_base_type: "base",
    DW_TAG_enumeration_type: "enum",
    DW_TAG_pointer_type: "pointer",
    DW_TAG_array_type: "array",
}


def uleb(buf, off):
    val = 0
    shift = 0
    while True:
        b = buf[off]
        off += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, off
        shift += 7


def sleb(buf, off):
    val = 0
    shift = 0
    while True:
        b = buf[off]
        off += 1
        val |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            if b & 0x40:
                val -= 1 << shift
            return val, off


class Elf:
    """Minimal ELF64 reader: sections, symbols, vaddr mapping."""

    def __init__(self, path):
        self.path = path
        self.f = open(path, "rb")
        ident = self.f.read(16)
        if ident[:4] != b"\x7fELF":
            raise ValueError("%s is not an ELF file" % path)
        if ident[4] != 2:
            raise ValueError("%s is not ELF64" % path)
        self.big = ident[5] == 2
        self.e = ">" if self.big else "<"
        self._sections = None

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
        (shoff,) = self._u("Q", 0x28, 8)
        shentsize, shnum, shstrndx = self._u("HHH", 0x3A, 6)
        raw = []
        for i in range(shnum):
            base = shoff + i * shentsize
            (name, typ, flags, addr, offset, size, link, info, align,
             entsize) = self._u("IIQQQQIIQQ", base, 64)
            raw.append({"name_off": name, "type": typ, "addr": addr,
                        "offset": offset, "size": size, "link": link,
                        "entsize": entsize})
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

    def read_section(self, name):
        s = self.section(name)
        if not s:
            return None
        self.f.seek(s["offset"])
        return self.f.read(s["size"])

    def read_range(self, secname, off, size):
        s = self.section(secname)
        if not s:
            return None
        self.f.seek(s["offset"] + off)
        return self.f.read(size)

    def scan_section(self, secname, needle, limit=None, chunk=1 << 24):
        """Chunked search for `needle`, returning section-relative offsets."""
        s = self.section(secname)
        if not s:
            return []
        hits = []
        overlap = len(needle) - 1
        pos = 0
        carry = b""
        self.f.seek(s["offset"])
        remaining = s["size"]
        while remaining > 0:
            n = min(chunk, remaining)
            blob = carry + self.f.read(n)
            base = pos - len(carry)
            start = 0
            while True:
                i = blob.find(needle, start)
                if i < 0:
                    break
                hits.append(base + i)
                if limit and len(hits) >= limit:
                    return hits
                start = i + 1
            pos += n
            remaining -= n
            carry = blob[-overlap:] if overlap else b""
        return hits


class Dwarf:
    def __init__(self, elf):
        self.elf = elf
        self.e = elf.e
        self._abbrev_cache = {}
        self._debug_str = None
        self._cus = None

    # ---- strings ----
    @property
    def debug_str(self):
        if self._debug_str is None:
            self._debug_str = self.elf.read_section(".debug_str") or b""
        return self._debug_str

    def str_at(self, off):
        b = self.debug_str
        if off >= len(b):
            return None
        end = b.find(b"\0", off)
        return b[off:end].decode("utf-8", "replace")

    # ---- CU index ----
    @property
    def cus(self):
        """[{off, length, version, abbrev_off, addr_size, dwarf64, first_die}]"""
        if self._cus is not None:
            return self._cus
        sec = self.elf.section(".debug_info")
        if not sec:
            self._cus = []
            return self._cus
        out = []
        off = 0
        size = sec["size"]
        while off + 11 < size:
            head = self.elf.read_range(".debug_info", off, 23)
            if not head or len(head) < 11:
                break
            (ul,) = struct.unpack(self.e + "I", head[0:4])
            dwarf64 = ul == 0xFFFFFFFF
            if dwarf64:
                (ul,) = struct.unpack(self.e + "Q", head[4:12])
                p = 12
                offsize = 8
            else:
                p = 4
                offsize = 4
            if ul == 0 or ul > size:
                break
            (version,) = struct.unpack(self.e + "H", head[p:p + 2])
            p += 2
            if version >= 5:
                # DWARF5: unit_type(1), address_size(1), abbrev_offset(offsize)
                unit_type = head[p]
                p += 1
                addr_size = head[p]
                p += 1
                abbrev_off = struct.unpack(
                    self.e + ("Q" if offsize == 8 else "I"),
                    head[p:p + offsize])[0]
                p += offsize
            else:
                abbrev_off = struct.unpack(
                    self.e + ("Q" if offsize == 8 else "I"),
                    head[p:p + offsize])[0]
                p += offsize
                addr_size = head[p]
                p += 1
            out.append({"off": off, "length": ul, "version": version,
                        "abbrev_off": abbrev_off, "addr_size": addr_size,
                        "dwarf64": dwarf64, "offsize": offsize,
                        "first_die": off + p,
                        "end": off + (12 if dwarf64 else 4) + ul})
            off = off + (12 if dwarf64 else 4) + ul
        self._cus = out
        return out

    def cu_for_offset(self, info_off):
        for cu in self.cus:
            if cu["off"] <= info_off < cu["end"]:
                return cu
        return None

    # ---- abbrev ----
    def abbrev(self, off):
        if off in self._abbrev_cache:
            return self._abbrev_cache[off]
        sec = self.elf.section(".debug_abbrev")
        if not sec:
            return {}
        blob = self.elf.read_range(".debug_abbrev", off,
                                   min(1 << 20, sec["size"] - off))
        table = {}
        p = 0
        try:
            while p < len(blob):
                code, p = uleb(blob, p)
                if code == 0:
                    break
                tag, p = uleb(blob, p)
                has_children = blob[p]
                p += 1
                attrs = []
                while True:
                    at, p = uleb(blob, p)
                    form, p = uleb(blob, p)
                    icst = None
                    if form == 0x21:            # DW_FORM_implicit_const
                        icst, p = sleb(blob, p)
                    if at == 0 and form == 0:
                        break
                    attrs.append((at, form, icst))
                table[code] = {"tag": tag, "children": bool(has_children),
                               "attrs": attrs}
        except IndexError:
            pass
        self._abbrev_cache[off] = table
        return table

    # ---- forms ----
    def read_form(self, blob, p, form, cu, icst=None):
        """Returns (value, new_p). Unknown forms raise."""
        e = self.e
        osz = cu["offsize"]
        if form == 0x01:                                  # addr
            n = cu["addr_size"]
            v = int.from_bytes(blob[p:p + n], "big" if self.elf.big else "little")
            return v, p + n
        if form == 0x03:                                  # block2
            (n,) = struct.unpack(e + "H", blob[p:p + 2]); p += 2
            return blob[p:p + n], p + n
        if form == 0x04:                                  # block4
            (n,) = struct.unpack(e + "I", blob[p:p + 4]); p += 4
            return blob[p:p + n], p + n
        if form == 0x05:                                  # data2
            return struct.unpack(e + "H", blob[p:p + 2])[0], p + 2
        if form == 0x06:                                  # data4
            return struct.unpack(e + "I", blob[p:p + 4])[0], p + 4
        if form == 0x07:                                  # data8
            return struct.unpack(e + "Q", blob[p:p + 8])[0], p + 8
        if form == 0x08:                                  # string (inline)
            end = blob.find(b"\0", p)
            return blob[p:end].decode("utf-8", "replace"), end + 1
        if form == 0x09:                                  # block
            n, p = uleb(blob, p)
            return blob[p:p + n], p + n
        if form == 0x0A:                                  # block1
            n = blob[p]; p += 1
            return blob[p:p + n], p + n
        if form == 0x0B:                                  # data1
            return blob[p], p + 1
        if form == 0x0C:                                  # flag
            return blob[p], p + 1
        if form == 0x0D:                                  # sdata
            return sleb(blob, p)
        if form == 0x0E:                                  # strp
            v = struct.unpack(e + ("Q" if osz == 8 else "I"),
                              blob[p:p + osz])[0]
            return ("strp", v), p + osz
        if form == 0x0F:                                  # udata
            return uleb(blob, p)
        if form == 0x10:                                  # ref_addr
            v = struct.unpack(e + ("Q" if osz == 8 else "I"),
                              blob[p:p + osz])[0]
            return ("gref", v), p + osz
        if form == 0x11:                                  # ref1
            return ("ref", blob[p]), p + 1
        if form == 0x12:                                  # ref2
            return ("ref", struct.unpack(e + "H", blob[p:p + 2])[0]), p + 2
        if form == 0x13:                                  # ref4
            return ("ref", struct.unpack(e + "I", blob[p:p + 4])[0]), p + 4
        if form == 0x14:                                  # ref8
            return ("ref", struct.unpack(e + "Q", blob[p:p + 8])[0]), p + 8
        if form == 0x15:                                  # ref_udata
            v, p = uleb(blob, p)
            return ("ref", v), p
        if form == 0x16:                                  # indirect
            f2, p = uleb(blob, p)
            return self.read_form(blob, p, f2, cu)
        if form == 0x17:                                  # sec_offset
            v = struct.unpack(e + ("Q" if osz == 8 else "I"),
                              blob[p:p + osz])[0]
            return v, p + osz
        if form == 0x18:                                  # exprloc
            n, p = uleb(blob, p)
            return blob[p:p + n], p + n
        if form == 0x19:                                  # flag_present
            return 1, p
        if form == 0x1A:                                  # strx
            v, p = uleb(blob, p); return ("strx", v), p
        if form == 0x1B:                                  # addrx
            v, p = uleb(blob, p); return v, p
        if form in (0x1F,):                               # line_strp
            v = struct.unpack(e + ("Q" if osz == 8 else "I"),
                              blob[p:p + osz])[0]
            return ("linestrp", v), p + osz
        if form == 0x20:                                  # ref_sig8
            return ("sig", struct.unpack(e + "Q", blob[p:p + 8])[0]), p + 8
        if form == 0x21:                                  # implicit_const
            return icst, p
        if form == 0x25:                                  # strx1
            return ("strx", blob[p]), p + 1
        if form == 0x26:                                  # strx2
            return ("strx", struct.unpack(e + "H", blob[p:p + 2])[0]), p + 2
        if form == 0x28:                                  # strx4
            return ("strx", struct.unpack(e + "I", blob[p:p + 4])[0]), p + 4
        if form in (0x29, 0x2A, 0x2B, 0x2C):              # addrx1..4
            n = {0x29: 1, 0x2A: 2, 0x2B: 3, 0x2C: 4}[form]
            return int.from_bytes(blob[p:p + n],
                                  "big" if self.elf.big else "little"), p + n
        raise ValueError("unsupported DW_FORM 0x%x" % form)

    def attr_str(self, val):
        if isinstance(val, tuple) and val[0] == "strp":
            return self.str_at(val[1])
        if isinstance(val, str):
            return val
        return None

    # ---- DIE walking ----
    def parse_cu(self, cu):
        """Parse a whole CU. Returns {die_offset: die} and a child index.

        A DIE is {off, tag, attrs, depth, children:[offsets]}. CUs here are
        large but bounded, and only CUs known to contain the target are parsed.
        """
        abbrev = self.abbrev(cu["abbrev_off"])
        if not abbrev:
            return {}
        start = cu["first_die"]
        size = cu["end"] - start
        blob = self.elf.read_range(".debug_info", start, size)
        if blob is None:
            return {}
        dies = {}
        stack = []
        p = 0
        while p < len(blob):
            die_off = start + p
            try:
                code, p = uleb(blob, p)
            except IndexError:
                break
            if code == 0:
                if stack:
                    stack.pop()
                continue
            ab = abbrev.get(code)
            if not ab:
                break                     # abbrev mismatch: stop, do not guess
            attrs = {}
            ok = True
            for at, form, icst in ab["attrs"]:
                try:
                    val, p = self.read_form(blob, p, form, cu, icst)
                except Exception:
                    ok = False
                    break
                attrs[at] = val
            if not ok:
                break
            die = {"off": die_off, "tag": ab["tag"], "attrs": attrs,
                   "depth": len(stack), "children": [],
                   "parent": stack[-1] if stack else None}
            dies[die_off] = die
            if stack and stack[-1] in dies:
                dies[stack[-1]]["children"].append(die_off)
            if ab["children"]:
                stack.append(die_off)
        return dies

    def die_name(self, die):
        return self.attr_str(die["attrs"].get(DW_AT_name))

    def resolve_ref(self, die, cu, dies):
        """Follow DW_AT_type to another DIE within the same CU when possible."""
        t = die["attrs"].get(DW_AT_type)
        if t is None:
            return None
        if isinstance(t, tuple):
            kind, v = t
            if kind == "ref":
                return dies.get(cu["off"] + v)
            if kind == "gref":
                return dies.get(v)
        return None

    def type_name(self, die, cu, dies, depth=0):
        """Best-effort readable type name, unwrapping qualifiers."""
        if die is None or depth > 8:
            return "?"
        nm = self.die_name(die)
        tag = die["tag"]
        if tag == DW_TAG_pointer_type:
            inner = self.resolve_ref(die, cu, dies)
            return (self.type_name(inner, cu, dies, depth + 1) + " *"
                    if inner else "void *")
        if tag in (DW_TAG_const_type, DW_TAG_volatile_type):
            inner = self.resolve_ref(die, cu, dies)
            q = "const " if tag == DW_TAG_const_type else "volatile "
            return q + self.type_name(inner, cu, dies, depth + 1)
        if nm:
            return nm
        if tag == DW_TAG_typedef:
            return self.type_name(self.resolve_ref(die, cu, dies), cu, dies,
                                  depth + 1)
        return TAG_NAMES.get(tag, "tag_0x%x" % tag)


# ------------------------------------------------------------------ layout ---
def bitfield_position(m_attrs, container_bytes):
    """Return (lsb, width, how) or None if the member is not a bitfield.

    DWARF 2/3: bit_offset counts from the HIGH-ORDER bit of the storage unit,
    per spec, independent of target endianness. DWARF 4+: data_bit_offset is
    from the start of the containing struct and needs no conversion.
    """
    bit_size = m_attrs.get(DW_AT_bit_size)
    if bit_size is None:
        return None
    dbo = m_attrs.get(DW_AT_data_bit_offset)
    if dbo is not None:
        return dbo, bit_size, "data_bit_offset"
    bit_off = m_attrs.get(DW_AT_bit_offset)
    if bit_off is None:
        return None
    unit = m_attrs.get(DW_AT_byte_size) or container_bytes or 4
    lsb = unit * 8 - bit_off - bit_size
    return lsb, bit_size, "byte_size=%d bit_offset=%d" % (unit, bit_off)


def member_location(attrs):
    v = attrs.get(DW_AT_data_member_location)
    if v is None:
        return 0
    if isinstance(v, int):
        return v
    if isinstance(v, (bytes, bytearray)):
        # An exprloc/block: DW_OP_plus_uconst (0x23) <uleb> is what gcc emits.
        if len(v) >= 2 and v[0] == 0x23:
            try:
                val, _ = uleb(v, 1)
                return val
            except IndexError:
                return 0
        return 0
    return 0


def describe_struct(dw, cu, dies, sdie, expand=0, _seen=None):
    """Decode a struct/union DIE into a layout record.

    `expand` recurses into members whose type is itself a struct or union,
    resolving them within the SAME CU. That matters: inner types here are named
    generically (schan_header_t's members are v2_s / v3_s / v4_s), so looking
    them up by name across 2264 CUs would match unrelated types. Following the
    DW_AT_type reference cannot pick the wrong one.
    """
    _seen = _seen or set()
    total = sdie["attrs"].get(DW_AT_byte_size)
    out = {"name": dw.die_name(sdie),
           "kind": TAG_NAMES.get(sdie["tag"], "?"),
           "byte_size": total, "cu_offset": cu["off"],
           "dwarf_version": cu["version"], "members": []}
    for coff in sdie["children"]:
        m = dies.get(coff)
        if not m or m["tag"] != DW_TAG_member:
            continue
        a = m["attrs"]
        mt = dw.resolve_ref(m, cu, dies)
        rec = {"name": dw.die_name(m), "type": dw.type_name(mt, cu, dies),
               "byte_offset": member_location(a)}
        bf = bitfield_position(a, total)
        if bf:
            lsb, width, how = bf
            rec.update({"bitfield": True, "lsb": lsb, "width": width,
                        "msb": lsb + width - 1, "derived_from": how})
        else:
            sz = mt["attrs"].get(DW_AT_byte_size) if mt else None
            rec.update({"bitfield": False, "byte_size": sz})
            inner = resolve_to_struct(dw, cu, dies, mt) if mt else None
            if expand > 0 and inner is not None and inner["off"] not in _seen:
                rec["inner"] = describe_struct(dw, cu, dies, inner,
                                               expand - 1,
                                               _seen | {inner["off"]})
        out["members"].append(rec)
    return out


def find_type_dies(dw, name, want_tags=(DW_TAG_structure_type,
                                        DW_TAG_union_type, DW_TAG_typedef),
                   max_cus=40, verbose=False):
    """Locate DIEs defining `name`. Returns [(cu, dies, die)]."""
    needle_offs = []
    target = name.encode()
    # 1. .debug_str offsets for this exact name
    sb = dw.debug_str
    pos = 0
    while True:
        i = sb.find(b"\0" + target + b"\0", pos)
        if i < 0:
            break
        needle_offs.append(i + 1)
        pos = i + 1
    if sb.startswith(target + b"\0"):
        needle_offs.append(0)

    cand_info_offs = []
    osz = 4          # DW_FORM_strp is 4 bytes in 32-bit DWARF, the normal case
    for so in needle_offs:
        pat = struct.pack(dw.e + "I", so) if osz == 4 else struct.pack(
            dw.e + "Q", so)
        cand_info_offs += dw.elf.scan_section(".debug_info", pat, limit=4000)
    # 2. inline DW_FORM_string occurrences
    cand_info_offs += dw.elf.scan_section(".debug_info", target + b"\0",
                                          limit=4000)
    if verbose:
        print("  name at %d .debug_str offset(s); %d candidate byte offset(s)"
              % (len(needle_offs), len(cand_info_offs)))

    seen_cu = []
    for off in sorted(set(cand_info_offs)):
        cu = dw.cu_for_offset(off)
        if cu and cu["off"] not in [c["off"] for c in seen_cu]:
            seen_cu.append(cu)
        if len(seen_cu) >= max_cus:
            break
    if verbose:
        print("  candidates fall in %d CU(s)" % len(seen_cu))

    hits = []
    for cu in seen_cu:
        dies = dw.parse_cu(cu)
        if not dies:
            continue
        for d in dies.values():
            if d["tag"] not in want_tags:
                continue
            if dw.die_name(d) != name:
                continue
            if d["attrs"].get(DW_AT_declaration):
                continue                    # a forward declaration has no layout
            hits.append((cu, dies, d))
    return hits


def resolve_to_struct(dw, cu, dies, die, depth=0):
    """Follow typedefs/qualifiers until a struct or union DIE is reached."""
    while die is not None and depth < 8:
        if die["tag"] in (DW_TAG_structure_type, DW_TAG_union_type):
            return die
        if die["tag"] in (DW_TAG_typedef, DW_TAG_const_type,
                          DW_TAG_volatile_type):
            die = dw.resolve_ref(die, cu, dies)
            depth += 1
            continue
        return None
    return None


def render(rec, indent="  "):
    lines = []
    lines.append("%s%s %s  (%s bytes, DWARF %s)"
                 % (indent, rec["kind"], rec["name"] or "<anon>",
                    rec["byte_size"], rec["dwarf_version"]))
    bits = [m for m in rec["members"] if m.get("bitfield")]
    if bits:
        for m in sorted(bits, key=lambda x: -x["lsb"]):
            span = ("[%d]" % m["lsb"] if m["width"] == 1
                    else "[%d:%d]" % (m["msb"], m["lsb"]))
            lines.append("%s  %-10s %-3s %-22s %s"
                         % (indent, span, m["width"], m["name"] or "?",
                            m["type"]))
        covered = sum(m["width"] for m in bits)
        total_bits = (rec["byte_size"] or 0) * 8
        if total_bits and covered != total_bits:
            lines.append("%s  NOTE: %d of %d bits accounted for -- %d "
                         "unaccounted (padding or a gap)"
                         % (indent, covered, total_bits, total_bits - covered))
    plain = [m for m in rec["members"] if not m.get("bitfield")]
    for m in plain:
        lines.append("%s  +0x%-4x %-22s %s%s"
                     % (indent, m["byte_offset"], m["name"] or "?", m["type"],
                        "" if m.get("byte_size") is None
                        else "  (%s bytes)" % m["byte_size"]))
        if m.get("inner"):
            lines.append(render(m["inner"], indent + "      "))
    return "\n".join(lines)


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

    # [1] ULEB128
    def t1():
        assert uleb(bytes([0x00]), 0) == (0, 1)
        assert uleb(bytes([0x7F]), 0) == (127, 1)
        assert uleb(bytes([0x80, 0x01]), 0) == (128, 2)
        assert uleb(bytes([0xE5, 0x8E, 0x26]), 0) == (624485, 3)
    grp("[1] uleb128", t1)

    # [2] SLEB128, including the sign-extension cases
    def t2():
        assert sleb(bytes([0x00]), 0) == (0, 1)
        assert sleb(bytes([0x02]), 0) == (2, 1)
        assert sleb(bytes([0x7E]), 0) == (-2, 1)
        assert sleb(bytes([0xFF, 0x00]), 0) == (127, 2)
        assert sleb(bytes([0x81, 0x7F]), 0) == (-127, 2)
        assert sleb(bytes([0x80, 0x01]), 0) == (128, 2)
    grp("[2] sleb128", t2)

    # [3] the bitfield conversion -- the thing most likely to be wrong.
    #     A 32-bit word, MSB-first bit_offset, must invert to LSB-based.
    def t3():
        # bit_offset 0, size 6, in a 4-byte unit: occupies the TOP 6 bits,
        # i.e. [31:26]
        r = bitfield_position({DW_AT_bit_size: 6, DW_AT_bit_offset: 0,
                               DW_AT_byte_size: 4}, 4)
        assert r[0] == 26 and r[1] == 6, r
        # the last bit of the unit: bit_offset 31, size 1 -> [0]
        r = bitfield_position({DW_AT_bit_size: 1, DW_AT_bit_offset: 31,
                               DW_AT_byte_size: 4}, 4)
        assert r[0] == 0 and r[1] == 1, r
        # a middle field: offset 7, size 7, 4-byte unit -> lsb 32-7-7 = 18
        r = bitfield_position({DW_AT_bit_size: 7, DW_AT_bit_offset: 7,
                               DW_AT_byte_size: 4}, 4)
        assert r[0] == 18 and r[1] == 7, r
        # DWARF4 data_bit_offset is used verbatim, not inverted
        r = bitfield_position({DW_AT_bit_size: 4, DW_AT_data_bit_offset: 12}, 4)
        assert r[0] == 12 and r[2] == "data_bit_offset", r
        # a non-bitfield member yields None
        assert bitfield_position({DW_AT_byte_size: 4}, 4) is None
        # container size is used when the member does not carry byte_size
        r = bitfield_position({DW_AT_bit_size: 2, DW_AT_bit_offset: 0}, 2)
        assert r[0] == 14 and r[1] == 2, r
    grp("[3] bitfield conversion", t3)

    # [4] gcc's DW_OP_plus_uconst member location
    def t4():
        assert member_location({DW_AT_data_member_location: 8}) == 8
        assert member_location(
            {DW_AT_data_member_location: bytes([0x23, 0x10])}) == 16
        assert member_location(
            {DW_AT_data_member_location: bytes([0x23, 0x80, 0x01])}) == 128
        assert member_location({}) == 0
    grp("[4] member location", t4)

    # [5] form reader over a synthesised buffer, big-endian
    def t5():
        class FakeElf:
            big = True
            e = ">"
        dw = Dwarf.__new__(Dwarf)
        dw.elf = FakeElf()
        dw.e = ">"
        cu = {"offsize": 4, "addr_size": 8}
        b = struct.pack(">I", 0x11223344)
        assert dw.read_form(b, 0, 0x06, cu) == (0x11223344, 4)      # data4
        assert dw.read_form(bytes([0xAB]), 0, 0x0B, cu) == (0xAB, 1)  # data1
        v, p = dw.read_form(b"hi\0rest", 0, 0x08, cu)                # string
        assert v == "hi" and p == 3, (v, p)
        v, p = dw.read_form(struct.pack(">I", 99), 0, 0x0E, cu)      # strp
        assert v == ("strp", 99) and p == 4, (v, p)
        v, p = dw.read_form(b"", 0, 0x19, cu)                       # flag_present
        assert v == 1 and p == 0
        v, p = dw.read_form(struct.pack(">I", 7), 0, 0x13, cu)       # ref4
        assert v == ("ref", 7)
    grp("[5] form reader", t5)

    # [6] render puts high bits first and flags an incomplete word
    def t6():
        rec = {"name": "t", "kind": "struct", "byte_size": 4,
               "dwarf_version": 3, "members": [
                   {"name": "lo", "type": "uint32", "byte_offset": 0,
                    "bitfield": True, "lsb": 0, "width": 8, "msb": 7,
                    "derived_from": "x"},
                   {"name": "hi", "type": "uint32", "byte_offset": 0,
                    "bitfield": True, "lsb": 24, "width": 8, "msb": 31,
                    "derived_from": "x"}]}
        out = render(rec)
        assert out.index("hi") < out.index("lo"), "not ordered MSB-first"
        assert "16 of 32 bits accounted for" in out, out
    grp("[6] render", t6)

    print("ffn_dwarfstruct selftest: %d groups, %d failed" % (groups, len(fails)))
    for f in fails:
        print("  FAIL " + f)
    return 1 if fails else 0


# --------------------------------------------------------------------- CLI ---
def main():
    ap = argparse.ArgumentParser(
        description="Recover a C struct's exact layout from a DWARF object")
    sub = ap.add_subparsers(dest="cmd")
    for c, h in (("find", "which CUs define this type"),
                 ("layout", "the struct layout, decoded")):
        p = sub.add_parser(c, help=h)
        p.add_argument("--elf", required=True)
        p.add_argument("--name", required=True, action="append",
                       help="type name; repeat for several")
        p.add_argument("--json", default=None)
        p.add_argument("--max-cus", type=int, default=40)
        p.add_argument("--depth", type=int, default=0,
                       help="expand nested struct/union members this deep")
        p.add_argument("--verbose", action="store_true")
    p = sub.add_parser("types", help="type names matching a pattern")
    p.add_argument("--elf", required=True)
    p.add_argument("--grep", required=True)
    p.add_argument("--limit", type=int, default=60)
    sub.add_parser("selftest", help="self-checks, no vendor data needed")

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
        dw = Dwarf(elf)
        if not elf.section(".debug_info"):
            print("%s has no .debug_info" % a.elf, file=sys.stderr)
            return 2

        if a.cmd == "types":
            rx = re.compile(a.grep)
            sb = dw.debug_str
            names = set()
            for m in re.finditer(rb"[\x20-\x7e]{2,120}", sb):
                s = m.group().decode("ascii", "replace")
                if rx.search(s):
                    names.add(s)
                    if len(names) >= a.limit * 4:
                        break
            for n in sorted(names)[:a.limit]:
                print("  " + n)
            print("(%d shown)" % min(len(names), a.limit))
            return 0

        print("CUs in .debug_info: %d" % len(dw.cus))
        results = []
        rc = 0
        for name in a.name:
            print()
            print("=== %s ===" % name)
            hits = find_type_dies(dw, name, max_cus=a.max_cus,
                                  verbose=a.verbose)
            if not hits:
                print("  not found as a defining struct/union/typedef DIE")
                rc = 1
                continue
            shown = 0
            for cu, dies, die in hits:
                sdie = resolve_to_struct(dw, cu, dies, die)
                if sdie is None:
                    continue
                rec = describe_struct(dw, cu, dies, sdie,
                                      expand=getattr(a, "depth", 0))
                rec["queried_as"] = name
                if a.cmd == "layout":
                    print(render(rec))
                else:
                    print("  CU 0x%x, DIE 0x%x, %s bytes"
                          % (cu["off"], sdie["off"], rec["byte_size"]))
                results.append(rec)
                shown += 1
                if shown >= 1 and a.cmd == "layout":
                    break            # identical copies per CU; one is enough
            if not shown:
                print("  found the name but no resolvable layout")
                rc = 1
        if a.json and results:
            with open(a.json, "w") as f:
                json.dump({"source": os.path.abspath(a.elf),
                           "structs": results}, f, indent=1, sort_keys=True)
            print()
            print("wrote %s" % a.json)
            print("This is derived vendor data. Keep it local: it must not "
                  "enter an FFN repository or image.")
        return rc
    finally:
        elf.close()


if __name__ == "__main__":
    sys.exit(main())
