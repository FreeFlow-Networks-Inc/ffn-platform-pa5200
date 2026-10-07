# CA1 BAR0 register map (measured)

Measured 2026-10-02 on our own PA-5220 CP through `/dev/ffn_ca10`, read-only,
using `ffn_ca1scan.py` and the structural passes in this directory's history.

Everything here is **observed behaviour of our own hardware** — register
offsets, block strides, instance counts, reset values. No vendor source was
used; `ca1.ko` carries no DWARF.

## Reading convention

**Use the big-endian accessors.** `ioread32()` is `le32_to_cpu(__raw_readl())`,
which on this big-endian MIPS CP swaps a second time on top of what the OCTEON
PCIe path already does, and every register comes out reversed.

The scratch register settles it: BAR0+0x0ec reads `0x33221100` via `ioread32()`
and `0x00112233` via `ioread32be()`. Every other live register agrees — one way
gives small sensible integers, the other gives noise:

| offset | `ioread32` | `ioread32be` |
|---|---|---|
| `+0x000` | `3f000000` | **`0000003f`** = 63 |
| `+0x014` | `18000000` | **`00000018`** = 24 |
| `+0x0b8` | `02000000` | **`00000002`** |
| `0x0c000` | `d2000000` | **`000000d2`** = 210 |
| `+0x0ec` | `33221100` | **`00112233`** |

The driver defaults to `big_endian=1`; all values below are in that convention.

**32-bit accesses only.** See the README — byte-wise access returns `0xff` and
makes the whole window look dead.

## Occupancy

256 KiB = 65536 words. **578 live** (neither `0x00000000` nor `0xffffffff`),
64934 zero, 24 all-ones. So the file is sparse — 0.9% populated.

| region | pages | live/page | contents |
|---|---|---|---|
| `0x00000` | 0 | 13 | global / identity block |
| `0x04000`–`0x07fff` | 4–7 | 60 | **24 × type-A blocks**, stride `0x200` |
| `0x08000`–`0x0bfff` | 8–11 | 54 | **24 × type-B blocks**, stride `0x200` |
| `0x0c000` | 12 | 4 | small control block |
| `0x28000` | 40 | 1 | single word |
| `0x2c000`–`0x2ffff` | 44–47 | 26 | type-C blocks |

## Block geometry

Stride is **`0x200`** (512 B). Confirmed by period analysis: `0x200` yields 17
distinct in-block offsets and `0x400` yields exactly 34 (= 2 × 17), so `0x200`
is the fundamental period, not a harmonic.

Both populated regions hold **24 instances**, but they are *not* fully packed —
some slots are deliberately empty:

- **`0x04000`–`0x07fff`**: gaps at `0x04600, 0x04e00, 0x05600, 0x05e00,
  0x06600, 0x06e00, 0x07600, 0x07e00` — i.e. in every `0x800` group of four
  blocks, the **4th is unused**. 8 groups × 3 = **24**.
- **`0x08000`–`0x0bfff`**: gaps at `0x08c00, 0x08e00, 0x09c00, 0x09e00` (and
  the same pattern onward) — in every `0x1000` group of eight, the **last two
  are unused**. 4 groups × 6 = **24**.

24 instances in each region is the strongest structural hint we have — it looks
like a per-channel or per-context register file, with the two regions being two
different block types over the same 24 units.

## Global block — `0x00000`

```
+0x000  0000003f     63 — capability/version-ish; 6 bits set
+0x004  00140001     looks like two 16-bit fields: 0x0014=20, 0x0001=1
+0x008  88000001
+0x014  00000018     24  <-- matches the instance count above
+0x018  00f0ffff     mask-shaped
+0x034  4121e000     VOLATILE — see below
+0x0b0  02020101     four byte-fields: 02 02 01 01
+0x0b8  00000002
+0x0e8  0000000f     15
+0x0ec  00112233     scratch / ID pattern
+0x140  00000908  }
+0x144  00000908  }  three identical, 0x908 = 2312
+0x148  00000908  }
```

`+0x014 = 24` matching the 24 block instances is almost certainly not a
coincidence — it reads like a "number of units" register.

## Type-A block — `0x04000`, 24 instances, stride `0x200`

All 24 are byte-identical (same live-offset signature *and* same values), i.e.
24 channels configured alike.

```
+0x004  00410000
+0x008  00002003
+0x00c  00000001
+0x010  00000001
+0x020  f0000f00     mask-shaped
+0x024  0000000f     15
+0x0a8  00960100
+0x0b8  000001ff     511 — a 9-bit field, or a size limit
+0x0d0  00000001
+0x0d4  00000001
```

Live-offset signature: `0x004 0x008 0x00c 0x010 0x020 0x024 0x0a8 0x0b8 0x0d0 0x0d4`

## Type-B block — `0x08000`, 24 instances, stride `0x200`

Different layout from type-A — it has `+0x000` and `+0x018` populated and
*lacks* the `0x004`–`0x010` group entirely:

```
+0x000  00000202
+0x018  00001000     4096
```

## Other regions

```
0x0c000:  +0x000 000000d2   210
          +0x004 00000705
          +0x00c 00d200d2   210 twice, packed as two 16-bit fields
          +0x010 000000d2   210

0x28000:  +0x000 00000072   114

0x2c000:  +0x000 0000001f   31
          +0x004 00039052
          +0x018 00000318   792
          +0x01c 0fffffff   28-bit mask
```

The recurrence of 210 at `0x0c000` in both scalar and packed-pair form suggests
a depth/size being reported in two places.

## The one volatile register

Across three passes two seconds apart, exactly **one** word in the whole 256 KiB
changes by itself:

```
+0x034   00e00041 -> 00e02140 -> 00e00140 -> 4121e000(*)
```

(*) later sample after the endian fix; the upper half is stable at `0x00e0…`
in the old convention, with the low bits moving.

One free-running register and 577 static ones is exactly what you would expect
from an engine that is **powered and configured but idle** — no traffic is being
pushed through it. That register is the natural place to look for a busy/activity
or counter field once we can drive work into the device.

## BAR1, BAR2, BAR3 (4 MiB each)

Swept in full — 1,048,576 words per BAR, about 5–9 s each.

| BAR | live words | what it is |
|---|---|---|
| 1 | 208 | **alias of the BAR0 global block**, repeated every `0x40000` |
| 2 | **0** | **4 MiB of uniformly zero, correctly-decoding RAM** |
| 3 | 208 | same aliasing as BAR1 |

### BAR1 and BAR3 are aliases, not new space

Each exposes only the 4 KiB **global block** — the same 13 live words as
BAR0 page 0 — repeated at `0x40000` (256 KiB) intervals: pages 0, 64, 128, …
960. Everything between is zero. They do **not** mirror BAR0's other regions:
reading BAR1 at `0x04000` (type-A), `0x0c000` or `0x2c000` returns all zeros
where BAR0 has content.

Verified word-by-word against BAR0:

```
BAR1+0         IDENTICAL to BAR0+0 (apart from +0x034)
BAR1+0x40000   IDENTICAL
BAR3+0         IDENTICAL
BAR3+0x40000   IDENTICAL
```

**Caveat worth recording:** a naive whole-page byte-compare says they *differ*,
because `+0x034` free-runs and changes between the two reads. Always exclude the
volatile register before concluding anything from a page compare.

So BAR1/BAR3 are 4 MiB apertures that currently decode only the low 4 KiB and
alias it upward — classic incomplete address decoding. They are presumably meant
to window something that is not configured in the device's present idle state.

### BAR2 is the table memory

4,194,304 bytes read as **uniformly zero** — sampled at `0x0000000`, `0x0001000`,
`0x0010000`, `0x0100000`, `0x0200000`, `0x0300000`, `0x03ff000`, `0x03fff00`, every
one all-zero, with **no all-ones anywhere**.

That distinction matters: all-ones would mean "not decoding". Zero across the
whole 4 MiB means the memory **is** present and addressable, and simply holds
nothing. This is almost certainly where the automaton goes — the vendor ships
Aho-Corasick data as `threats/av/aho` and compiled match tokens as
`tdb_fpga_token_*`, and the vendor driver exposes `nac_aho_read/write` and
`nac_dfa_read/write` over exactly this kind of arena, with three DDR layouts
(`small`/`medium`/`large`).

### BAR2 write test — writes do NOT stick

Run 2026-10-02 with the module loaded `allow_write=1`. Wrote `deadbeef`,
`5a5a5a5a`, `a5a5a5a5`, `01234567` to `+0x000000`, `+0x001000`, `+0x100000`,
`+0x3ffff0`. Each `write()` returned 4 bytes with no error. **All four read back
`00000000`.** Restored and re-verified zero; no bus errors, CP healthy.

So BAR2 decodes, reads zero across the whole 4 MiB, and **silently discards host
writes**.

The most likely reading is that the arena is not enabled in the device's idle
state and that table loading is not raw MMIO at all. That fits the vendor
surface precisely:

- `nac_mem_take` / `nac_mem_release` and `nac_ddr_layout_small/medium/large`
  imply the arena must be **allocated and configured** before use;
- `nac_aho_write` and `nac_dfa_write` being *functions* implies **indirect,
  register-mediated** access (address register + data register), not direct
  stores into the window.

Practical consequence: chase the BAR0 registers first. Do not expect to push
automaton tables straight into BAR2.

## Summary of the whole device

```
BAR0  256 KiB   the register file (global + 24 x type-A + 24 x type-B + small blocks)
BAR1    4 MiB   alias of the global block every 0x40000; otherwise zero
BAR2    4 MiB   empty table RAM  <-- the automaton arena
BAR3    4 MiB   alias of the global block every 0x40000; otherwise zero
```

## What is still unknown

- Register *semantics*. Nothing here is named; these are measured values.
- Which block type corresponds to the Aho-Corasick path versus the DFA path.
- Whether BAR2 is writable RAM (needs the write-readback test above).
- What BAR1/BAR3 are meant to window once the device is configured.

## Reproducing

```sh
ffn_ca1scan.py info
ffn_ca1scan.py dump --bar 0 --len 512
ffn_ca1scan.py live --bar 0
ffn_ca1scan.py volatile --bar 0
```

A full 256 KiB BAR0 sweep takes **~0.2 s** (about 2 µs/word) now that accesses
are 32-bit. Before that fix the same sweep would have taken roughly six hours.
