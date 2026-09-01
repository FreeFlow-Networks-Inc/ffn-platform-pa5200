#!/usr/bin/env python3
"""Which function in the soc_dpp_init path takes a mutex it never created?

sync.c:554 is the assert inside sal_mutex_take that its handle is non-NULL, so
something calls sal_mutex_take(NULL). This counts direct jal calls to
sal_mutex_take and sal_mutex_create inside each function on the init path, to
narrow down which subsystem is taking an uncreated mutex.
"""
import sys, struct, importlib.util

spec = importlib.util.spec_from_file_location("m", "/home/stephen/ffn_bcmregs.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

elf = m.Elf(sys.argv[1])
take = elf.symbols["sal_mutex_take"][0]
create = elf.symbols["sal_mutex_create"][0]
sem_take = elf.symbols.get("sal_sem_take", (0, 0))[0]


def jalword(t):
    return struct.pack(">I", 0x0C000000 | ((t >> 2) & 0x03FFFFFF))


CANDIDATES = [
    "soc_dpp_init",
    "soc_dpp_implementation_defines_init",
    "soc_arad_default_config_get",
    "soc_arad_dram_param_set",
    "soc_dpp_wb_engine_init",
    "soc_arad_init",
    "soc_dpp_jericho_init",
    "soc_dpp_attach",
    "shr_sw_state_init",
]

print("  %-40s %8s %6s %8s %8s" % ("function", "bytes", "take", "create", "semtake"))
for fn in CANDIDATES:
    a, sz = elf.symbols.get(fn, (0, 0))
    if not a or not sz:
        print("  %-40s not found" % fn)
        continue
    off, _sec = elf.vaddr_to_offset(a, sz)
    if off is None:
        print("  %-40s unmapped" % fn)
        continue
    elf.f.seek(off)
    blob = elf.f.read(sz)
    t = blob.count(jalword(take))
    c = blob.count(jalword(create))
    s = blob.count(jalword(sem_take)) if sem_take else 0
    flag = "   <-- takes without creating" if t and not c else ""
    print("  %-40s %8d %6d %8d %8d%s" % (fn, sz, t, c, s, flag))
