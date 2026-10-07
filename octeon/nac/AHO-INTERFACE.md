# CA1 Aho-Corasick hardware interface

Recovered 2026-10-02 by disassembling `nac_ioctl` in the vendor's `ca1.ko`
(MIPS64 BE, no DWARF) with the kernel.org cross objdump. These are **interface
facts about the hardware** — register offsets, strobe bits, field packing. No
vendor code is reproduced.

## Scope note

The vendor's *signature content* is encrypted: `threats/av/aho/all.fnc`
measures entropy **7.998** over 95 KB with all 256 byte values present and no
compression magic. We do not attempt to decrypt it — that is licensed
threat-intelligence data, not hardware. What is documented here is the
**hardware interface**, so FFN can compile and load its **own** automata.
Aho-Corasick itself is a published algorithm (Aho & Corasick, 1975); nothing
about it needs reversing.

## The engine: four CIP units

`nac_init` reads capability registers at **BAR0 + 0x8000 / 0x9000 / 0xA000 /
0xB000** (`CIP_CIP_CAP_ADDR(0..3)`), all reading `0x00000202` on our hardware.
So the matcher is **four units on a 0x1000 stride**.

## The complete MMIO write surface

Across the *entire* driver these are the only registers written:

| register | role |
|---|---|
| `BAR0 + 0x100` | global arena enable — bit 0 (type-1 regions), bit 16 (type-2) |
| `unit + 0x100` | **indexed read port** |
| `unit + 0x110` | **table write port** |
| `unit + 0x120` | **configuration word** |

where `unit` ∈ { `0x8000`, `0x9000`, `0xA000`, `0xB000` }. Twelve per-unit
registers plus one global. Nothing else in the window is ever written.

## `unit + 0x110` — loading the state-transition table

The loader walks all four units per input byte:

```asm
li     v1, 0x8110          ; first unit's write port
li     a4, 0xc110          ; bound (one past 0xB110)
loop:
  lbu    v0, 0(a1)         ; next byte of the caller's buffer
  ld     a0, 16(s0)        ; BAR0 base  (struct+16)
  andi   v0, v0, 0x7       ; keep the low 3 bits
  daddu  a0, a0, v1
  or     v0, v0, t2        ; OR in row index + flags
  sw     v0, 0(a0)         ; write this unit
  daddiu v1, v1, 4096      ; 0x8110 -> 0x9110 -> 0xA110 -> 0xB110
  bne    v1, a4, loop
  daddiu a1, a1, 1         ; advance the input byte
  ...
  sw zero, 0x8110 / 0x9110 / 0xA110 / 0xB110    ; terminate the load
```

**Written word layout** (`t0 = 0x10000` is added to the row counter each outer
pass, and masked with `0xff0000`):

```
 bits [2:0]    the input byte, masked to 3 bits
 bits [23:16]  row / state index
 other bits    flag fields carried in from the caller's parameters
```

A load ends by writing **zero to `+0x110` in all four units**.

## `unit + 0x100` — reading the table back

A strobed indexed read. Bit 15 is the strobe:

```asm
and   v1, a1, t1           ; index, masked
or    v1, v1, a7           ; flags
ori   a2, v1, 0x8000       ; set bit 15 = strobe
sw    a2, 0x8100(base)     ; write index WITH strobe
sw    v1, 0x8100(base)     ; write index WITHOUT strobe
...
andi  v1, v1, 0xfff        ; result is 12 bits
sh    v1, -2(a5)           ; store back to the caller as a halfword
...
sw    zero, 0x8100(base)   ; terminate
```

So: assert index|0x8000, deassert, read a **12-bit** value. Results are returned
to userspace as halfwords, and the sequence terminates with a zero write.

## `unit + 0x120` — configuration

One packed word written **identically to all four units**:

```asm
or    v1, v1, a6
dsll  a0, a0, 2            ; a field at bits [3:2]
sltiu a1, a1, 1
dsll  a1, a1, 4            ; a boolean at bit 4
or    v1, v1, a0
or    v1, v1, a1
sw    v1, 0x8120 / 0x9120 / 0xA120 / 0xB120
```

Written from the `_IOW('N', 0x03, 4)` handler, so it is set by a 4-byte write
ioctl.

## The ioctl ABI

Type **`'N'` (0x4E)**, twelve commands, nr `0x00`–`0x0B`. MIPS encoding —
direction at bit **29** (not 30), `NONE=1 / READ=2 / WRITE=4`, size 13 bits at
16. Identified dispatch targets:

| nr | direction | handler |
|---|---|---|
| `0x00` | | after `0xd5c` |
| `0x01` | | `0x13d8` |
| `0x02` | `_IOR` | `0xff8` |
| `0x03` | `_IOW` | `0x1298` — writes the `+0x120` config word |
| `0x04` | | `0x11d0` |
| `0x08` | | `0x1090` |
| `0x09` | `_IOR` | falls through at `0x994` |
| `0x0A` | `_IOR` | `0xef8` |
| `0x0B` | | range bound |

`0xff0` and `0x1c18` are the error returns. There is also one 260-byte command.

## On "reset" — it is not a CA1 register

There is **no dedicated reset register**. The driver's complete write set is the
twelve per-unit registers plus the global arena enable; none of them is a reset.
The zero-writes at `+0x100` / `+0x110` are *sequence terminators*, not resets.

So `ca1lib.ca1_reset()` / `ca1_clk_reset()` must act somewhere else. The strong
candidate is the **CE CPLD** — the same path `fpga_program` streams through, and
the vendor's `/sbin/octeon` probes it with `ce_cpld_reg read 0x2` before
programming. `ca1lib` is not present on this disk, so this is inference from
the absence of any reset in the kernel driver, not a direct reading.

That also fits what we measured: `BAR0+0x0B8` bit 0 ("memory channels init") is
read exactly once in the whole driver and never written, and reprogramming the
FPGA left every register bit-identical. Whatever trains the DDR is outside both
the FPGA bitstream and this register file.

## What FFN needs to build

1. An Aho-Corasick automaton compiler (public algorithm, our own patterns).
2. An emitter producing the `+0x110` word format above, written across all four
   units per input byte and zero-terminated.
3. The `+0x120` configuration word — field meanings still to be determined.
4. Readback verification via the `+0x100` strobed port.
5. The DDR arena enabled first — blocked on `BAR0+0x0B8` bit 0, see
   `ACCESS-PROTOCOL.md`.
