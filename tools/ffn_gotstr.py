#!/usr/bin/env python3
"""Resolve the per-index mutex name table used by soc_dpp_attach's 7th create site.

The site does:
    ld     v1, -14344(gp)     ; a table of char*
    lw     v0, 32(s8)         ; index i
    dsll   v0, v0, 3
    daddu  v0, v1, v0
    ld     v0, 0(v0)          ; name = table[i]
    jal    sal_mutex_create

gp for soc_dpp_attach was already computed as 0x1a4d23b8.
"""
import sys, struct, importlib.util

spec = importlib.util.spec_from_file_location("m", "/home/stephen/ffn_bcmregs.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
elf = m.Elf(sys.argv[1])

GP = 0x1A4D23B8


def cstr(addr, n=64):
    off, _sec = elf.vaddr_to_offset(addr, 1)
    if off is None:
        return None
    elf.f.seek(off)
    c = elf.f.read(n)
    e = c.find(b"\0")
    return c[:e].decode("ascii", "replace") if e > 0 else None


def gotptr(neg):
    raw = elf.read_at_vaddr(GP - neg, 8)
    if not raw:
        return None
    return struct.unpack(">Q", raw)[0]


# reverse symbol map so we can name whatever the table turns out to be
rev = {}
for nm, (a, s) in elf.symbols.items():
    if a and nm not in ("",):
        rev.setdefault(a, nm)

for slot, what in ((14344, "7th create name table"),
                   (15392, "soc_control array"),
                   (14392, "sal_mutex_create thunk")):
    p = gotptr(slot)
    print("GOT[-%d] -> 0x%x   %s   symbol=%s"
          % (slot, p or 0, what, rev.get(p, "(none)")))

tbl = gotptr(14344)
print()
print("  entries of the name table:")
for i in range(12):
    raw = elf.read_at_vaddr(tbl + i * 8, 8)
    if not raw:
        break
    (p,) = struct.unpack(">Q", raw)
    nm = cstr(p) if p else None
    print("    [%2d] 0x%016x  %r" % (i, p, nm))
    if p == 0 and i > 2:
        break
