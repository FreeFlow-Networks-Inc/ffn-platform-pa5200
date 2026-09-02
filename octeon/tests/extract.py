#!/usr/bin/env python3
"""Extract the ffn_reserve code verbatim from the patched setup.c so the unit
test exercises the real shipped source rather than a retyped copy."""
import io
import sys

SRC = ("/mnt/clones/sdk51/OCTEON-SDK/linux/kernel/linux/"
       "arch/mips/cavium-octeon/setup.c")
OUT = "/tmp/ffnblk/ffn_extract.inc"

lines = io.open(SRC, encoding="utf-8", newline="").read().split("\n")


def find(pred, start=0):
    for n in range(start, len(lines)):
        if pred(lines[n]):
            return n
    sys.exit("marker not found")


# Block A: the struct + statics
a0 = find(lambda l: l.startswith("#define FFN_RESERVE_MAX"))
a1 = find(lambda l: l.startswith("static int ffn_reserve_count"), a0)

# Block B: ffn_reserve_next_entry() .. end of memory_exclude_range().
# Start at the helper, not at ffn_reserve_parse(), or the extracted file calls a
# function it has not declared.
b0 = find(lambda l: l.startswith("static __init const char *ffn_reserve_next_entry"))
hit = find(lambda l: l.strip() == "r->hit += drop;", b0)
b1 = find(lambda l: l == "}", hit)

# Block C: the ffn_mem=auto helpers (spliced after memory_exclude_range)
c0 = find(lambda l: l.startswith("#define FFN_MEM_MARGIN"))
cend = find(lambda l: l.strip().startswith("pr_info(\"FFN: if the FPA pools"), c0)
c1 = find(lambda l: l == "}", cend)

out = ("\n".join(lines[a0:a1 + 1]) + "\n\n"
       + "\n".join(lines[b0:b1 + 1]) + "\n\n"
       + "\n".join(lines[c0:c1 + 1]) + "\n")
io.open(OUT, "w", encoding="utf-8", newline="").write(out)
print("extracted %d lines (A %d-%d, B %d-%d, C %d-%d) -> %s"
      % (out.count("\n"), a0 + 1, a1 + 1, b0 + 1, b1 + 1, c0 + 1, c1 + 1, OUT))
