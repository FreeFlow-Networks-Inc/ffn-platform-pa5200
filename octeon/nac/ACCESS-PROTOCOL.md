# CA1 access protocol

Recovered 2026-10-02 by disassembling the vendor's `ca1.ko` (25 KB, MIPS64 BE)
with the kernel.org cross objdump. These are **interface facts about the
hardware** — which BAR is which, access width, byte order, probe ordering. No
vendor code is reproduced here.

Source: `/mnt/clones/5220-fe100-bcm-drivers-11.2.4-h5/kmod-mips/ca1.ko`.

## There is no address/data register pair

This was the working theory and it is **wrong**. All four table accessors are
direct `base + offset` loads and stores:

```
nac_aho_write:  ld v0,24(a0)   wsbh a2,a2 ; ror a2,a2,0x10   daddu v0,v0,a1   sw a2,0(v0)
nac_aho_read:   ld v0,24(a0)   lwx v0,a1(v0)                 wsbh v0,v0 ...
nac_dfa_write:  ld v0,32(a0)   wsbh a2,a2 ; ror a2,a2,0x10   daddu v0,v0,a1   sw a2,0(v0)
nac_dfa_read:   ld v0,32(a0)   lwx v0,a1(v0)                 wsbh v0,v0 ...
```

`wsbh` + `ror …,0x10` is the MIPS idiom for a full 32-bit byteswap. So each is
simply: byteswap, then a direct 32-bit store at `window_base + offset`.

## BAR → window map

From the `pci_iomap(pdev, N, 0)` sequence in `nac_probe` and the struct offsets
the results are stored to:

| BAR | struct off | window | used by |
|---|---|---|---|
| 0 | +16 | **registers** | layout select, control |
| 1 | +24 | **AHO table** | `nac_aho_read` / `nac_aho_write` |
| 2 | +32 | **DFA table** | `nac_dfa_read` / `nac_dfa_write` |
| 3 | +40 | — | not touched by the four accessors |

So the 4 MiB apertures are **two different table memories**, not one arena plus
padding: BAR1 holds the Aho-Corasick automaton, BAR2 holds the DFA.

## Byte order differs BY WINDOW — this is the subtle part

| window | vendor access | our accessor |
|---|---|---|
| BAR0 registers | **raw `lw`/`sw`**, no swap → big-endian | `ioread32be` / `iowrite32be` |
| BAR1/BAR2 tables | `wsbh`+`ror` byteswap → little-endian | `ioread32` / `iowrite32` |

The register path is visible at `nac_probe+0x174`: `lw v0,8(v0)` on the BAR0
base with **no** `wsbh`, while every table accessor swaps. This independently
confirms the BAR0 endianness we derived empirically from the scratch register
(`+0x0ec` = `00112233` read big-endian), and it means a driver must use
**different accessors per BAR** — one global setting is wrong.

## 32-bit only

Every access in the vendor driver is `lw`/`sw`/`lwx` — word-sized. Nothing does
byte or halfword access. This matches what we measured: sub-word access returns
`0xff` and makes the whole window look dead.

## The DDR layout selector

```asm
ld   v0, 16(s2)          ; BAR0 base
lw   v0, 8(v0)           ; BAR0 + 0x008, raw big-endian
dext v0, v0, 0x1e, 0x2   ; bits [31:30]
lhux a1, v0(a1)          ; index a halfword table
beq  a1, 2 / 3 / 1       ; three-way branch
```

**BAR0 + 0x008, bits [31:30], selects the DDR layout** — the three-way branch
corresponds to the `nac_ddr_layout_small` / `_medium` / `_large` data objects
(`.data` sizes 0x30 / 0x3c / 0x54 respectively).

Our hardware reads BAR0+0x008 = `0x88000001`, so bits[31:30] = `0b10` = **2**.

## Probe ordering

```
pci_enable_device
pci_request_regions
__get_free_pages(order 3)        -- 8 pages = 32 KiB for the device struct
pci_iomap(pdev, 0..3, 0)         -- stored to struct +16/+24/+32/+40
__register_chrdev(..., 256 minors)
read BAR0+0x008 -> pick DDR layout
```

`pci_enable_device` first matches what our own driver does, and is what turns
memory decode on.

## Why our BAR2 write test failed

We wrote to BAR2 — which this now identifies as the **DFA window** — and the
writes were discarded. Two things were wrong with that test:

1. We used big-endian accessors on a window that the vendor accesses
   **little-endian**. That alone would corrupt values, though it would not
   explain them reading back as zero.
2. More importantly, the arena is almost certainly not enabled until the device
   is configured: `nac_mem_take` / `nac_mem_release` exist precisely to allocate
   it, and the DDR layout has to be selected first.

So the order of business is: read the layout selector, understand
`nac_mem_take`, and only then expect the table windows to accept data.

## The DDR arena — geometry and the enable gates

CA1 has **hundreds of MB of attached DDR**. The 4 MiB BARs are *windows* into
it, not the storage; that is the right scale for Aho-Corasick automata.

### The three layout tables

`.data` holds three arrays of **3-word entries** — `{ size, (u16,u16), type }`.
The 3-word stride is confirmed by `nac_mem_take`, which indexes with
`index*3 << 2` = `index*12`.

| layout | entries | regions | total |
|---|---|---|---|
| `nac_ddr_layout_small` | 4 | 8, 8, 32, 128 MiB | **176 MiB** |
| `nac_ddr_layout_medium` | 5 | 16, 16, 32, 128, 128 MiB | **320 MiB** |
| `nac_ddr_layout_large` | 7 | 32, 32, 64, 128×4 MiB | **640 MiB** |

The `(u16,u16)` pair is `1:1`, `1:2` or `1:4` — interleave or bank count. The
third word is the region **type**, and it is only ever 1 or 2; it selects which
enable bit the region uses (below).

### Which layout this board runs

```
BAR0+0x008 bits[31:30]                  = 2        (measured on our hardware)
  -> .rodata halfword table [3, 2, 1, 3], index 2  = 1
  -> branch sets entry count 4 and points struct+48 at the layout
  -> nac_ddr_layout_small
```

The three branches set counts **4 / 5 / 7**, which match the three tables'
`.data` sizes exactly (0x30, 0x3c, 0x54 ÷ 12). So value 1 → small, 2 → medium,
3 → large.

**Our PA-5220 runs the SMALL layout: 4 regions, 176 MiB.**

### The enable gates

`nac_mem_take(dev, index)` and `nac_mem_release(dev, index)` are exact mirrors:

```
take:     lw   BAR0+0x0B8 ; if bit 0 CLEAR -> bail, -EIO
          entry.type == 1 -> SET   bit 0  of BAR0+0x100
          entry.type == 2 -> SET   bit 16 of BAR0+0x100
          else            -> -EINVAL
          read back BAR0+0x100          (posted-write flush)

release:  entry.type == 1 -> CLEAR bit 0  of BAR0+0x100   (and v0, -2)
          entry.type == 2 -> CLEAR bit 16 of BAR0+0x100   (and v0, 0xfffeffff)
          read back BAR0+0x100
```

Note `release` does **not** check the ready gate; only `take` does.

| register | meaning | measured |
|---|---|---|
| `BAR0+0x0B8` bit 0 | memory **ready** gate | `0x00000002` → **0, NOT ready** |
| `BAR0+0x100` bit 0 | arena enable, type-1 regions | `0x00000000` → off |
| `BAR0+0x100` bit 16 | arena enable, type-2 regions | `0x00000000` → off |

**This is the complete explanation for the failed BAR2 write test.** The ready
gate is clear, so `nac_mem_take` would itself return `-EIO`, and both arena
enables are off — the table windows cannot accept data in this state. Nothing
was wrong with the write path.

### What sets `BAR0+0x0B8` bit 0 — answered: nothing in software does

**It is a read-only hardware status bit.** Proof from the driver itself:

```
every access to offset 184 (0xB8) in ca1.ko:
     2cc:  8cc200b8   lw  v0,184(a2)      <- ONE access, a READ

every MMIO store offset in the entire driver:
     256 (0x100)                          <- only the arena enable
```

One read, no writes, anywhere. And the driver's own string table names the
failure: **`"%s: Memory channels init incomplete"`** is the printk on that
branch. So bit 0 is the CA1 **FPGA's memory controller** reporting that it has
finished initialising/training its DDR channels.

**It was not brdagent.** Both `brdagent` binaries (`/opt/dpfs/usr/local/{cp,dp}/brdagent`)
contain **zero** references to `nac` or `ca1`. Nor is it `ca1_setup`, which is a
413-byte shell script that only walks `/sys/class/nac*` and `mknod`s the device
nodes.

### CA1 is an FPGA, and it is driven from the DP — not the CP

The string table settles both points:

```
"%s: FPGA version too old (%d < %d) - aborting!"
"%s - version %s (fpga version %d)"
"1.0-ca1"
```

and `pdt/fpga_ca1.py` registers for the dataplane only:

```python
cmd = "oncpu 0x03 pg_ca --count=%d --channel=%d ..."   # ilk channel (0/1/2)
print("DP to CA1 packet test passed")
pdt.cli.add_class(fpga_ca1, module=False, roles=['dp'])   # family 5200
```

**CA1 is fed by the DP over Interlaken channels 0/1/2.** The CP is a CN73XX and
has no Interlaken at all, so it can never push traffic at CA1 — it can only
reach the register file over PCIe, which is exactly what our driver does. This
corrects the long-standing "ca1 not loadable on the 5220" note: the part is
present and its registers answer; it is simply a *dataplane* accelerator.

### The CIP block

`nac_init` reads four capability registers:

```asm
lui v0, 0x1
lw  a1, -32768(v0)   ; BAR0 + 0x8000     CIP_CIP_CAP_ADDR(0)
lw  a2, -28672(v0)   ; BAR0 + 0x9000     CIP_CIP_CAP_ADDR(1)
lw  a3, -24576(v0)   ; BAR0 + 0xA000     CIP_CIP_CAP_ADDR(2)
lw  a4, -20480(v0)   ; BAR0 + 0xB000     CIP_CIP_CAP_ADDR(3)
```

So the region we mapped as "24 × type-B blocks at `0x08000`–`0x0bfff`" is the
**CIP block — four units at 0x1000 stride**. Measured on our hardware, all four
read **`0x00000202`**, i.e. the FPGA is answering with real, consistent
capability data. Other status strings in the same family:
`CIP_EMPTY_STATUS0/1/2` and `DFA_EMPTY_STATUS0/1/2/3`.

### So why are the memory channels not initialised

CA1's register file is alive and self-consistent, so a bitstream *is* loaded.
What has not happened is DDR channel init. The likely candidates, in order:

1. The full bitstream (`ca1.bin`, ~24.6 MB on the FFN-VENDOR volume) has not
   been loaded — only enough fabric to answer PCIe and the register file. This
   is the same situation as `ce40` before we learned to load it ourselves.
2. DDR init is performed by a DP-side bring-up step that never runs because we
   boot the DP ourselves and skip the vendor sequence.

Either way it is **not** something the CP can poke into place through
`BAR0+0x0B8`.

## Consequences for `ffn_ca1`

- The single `big_endian` module parameter is **too coarse**. Byte order is a
  property of the window, not the device: BAR0 big-endian, BAR1/BAR2
  little-endian. This needs to become per-BAR.
- BAR1 is not "an alias of the global block" in any meaningful sense — it is the
  AHO window, which in the device's unconfigured state happens to shadow the
  register block. Same for BAR3's behaviour.
