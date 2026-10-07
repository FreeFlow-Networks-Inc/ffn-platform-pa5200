# CE40 (feed:a00d) BAR0 register map — measured

Read 2026-10-06 from our own PA-5220, through `ffn_ca1` bound to `0003:06:00.0`
by PCI `new_id`, immediately after a clean boot through the production path
(`ffn-octeon-up.sh` with the FPGA step: ce40.bin programmed, Linux booted with
no Octeon reset in between). Raw data: [measured/a00d-bar0-20261006.bin](measured/a00d-bar0-20261006.bin)
(256 KiB) and [measured/a00d-bar0-live-20261006.txt](measured/a00d-bar0-live-20261006.txt)
(every word that is neither `0x00000000` nor `0xffffffff`: **341** of 65,536).

Values are the decoded big-endian dwords (BAR0 is natively big-endian — the
vendor's `pan_pcimap_wr32` is a bare `sw`). This is the **a00d personality**;
[BAR0-MAP.md](BAR0-MAP.md) is the a101 personality of the same socket and differs
from the first word onward.

Labels marked *PDT* come from register names in the vendor's readable
`ce40.py`; labels marked *vendor init* from the `ce40_init()` sequence
recovered in [CE40-BRINGUP.md](CE40-BRINGUP.md); everything else is a measured
value with a structural reading, not a name.

## Head block (`0x0000`–`0x0fff`, 13 live)

| offset | value | reading |
|---|---|---|
| `0x000` | `0x00000013` | **FPGA version 19** — the number PAN-OS's `ce40.ko` logs (`fpga version 19`); *vendor init* reads it and checks a minimum |
| `0x004` | `0x00140001` | |
| `0x008` | `0x88000007` | *vendor init* stores `value >> 30` = **2** as the DDR layout selector (`CE40_MDL_SMALL/MEDIUM/LARGE`) |
| `0x018` | `0x00f00047` | *PDT* names a register here |
| `0x0b0` | `0x02020101` | |
| `0x0b8` | `0x000000e2` | **RLD calibration status** — PAN-OS logs exactly `FPGA RLD CAL 0xe2, type 1`; on a101 this read `0x02` and was mis-read as "memory channels init incomplete" |
| `0x0e8` | `0x0000000f` | |
| `0x0ec` | `0x00112233` | scratch / ID pattern, identical on a101 |
| `0x108`, `0x120` | `0x000007ff` | 11-bit masks |
| `0x10c`, `0x124` | `0x000007f8` | |
| `0x128` | `0x1f00422b` | |

`0x010` (the reset register on a101) reads `0`.

## Interlaken link blocks (`0x4000`–`0x7fff`, 4 × 56 live)

Blocks of `0x200`, exactly the *PDT*'s `address=0x4004 + 0x200*link`. Thirty-two
slots; 24 carry the full register set, 16 carry the `0x080`/`0x08c` pair.

| offset in block | value | count |
|---|---|---|
| `0x004` | `0x00410000` | 24 |
| `0x008` | `0x00002003` | 24 |
| `0x00c` | `0x00000001` | 24 |
| `0x010` | `0x00000015` | 24 |
| `0x020` | `0x10000f00` | 24 |
| `0x024` | `0x00000001` | 24 |
| `0x080`, `0x08c` | `0xfffff800` | 16 |
| `0x084`, `0x090` | `0x00000007` | 24 (`0x000007ff` on the other 8) |

The *PDT* reads `0x4054/0x4058/0x4068/0x406c + i*0x200` as 64-bit counter
pairs; those are zero here (no traffic yet).

## CIP units (`0x8000`–`0xbfff`, 4 × 13 live)

Four identical units at `0x1000` stride — the "4 CIP units" of
[AHO-INTERFACE.md](AHO-INTERFACE.md). Per unit:

| offset in unit | value |
|---|---|
| `0x000` | `0x07000000` |
| `0x004` | `0x00006000` |
| `0x00c` | `0x50000000` |
| `0x010`, `0x040`, `0x070`, `0x0e0`, `0x0f0`, `0x170`, `0x190` | `0x00ffffff` |
| `0x0b0` | `0x003fffff` |
| `0x0d0` | `0x20000000` |
| `0x120` | `0x00000018` (the unit's config word per AHO-INTERFACE.md) |

The *PDT*'s `cip_partition_offset = 0x8800 + 0x1000*i` lands in the second half
of each unit, which reads all-zero: partitions are unconfigured.

## DFA (`0xc000`)

`0xc000 = 0x80000003`, `0xc00c = 0x80000000`. The *PDT*'s
`dfa_partition_offset = 0xc800 + 0x1000*i` region reads zero.

## Memory-controller blocks

| offset | value |
|---|---|
| `0x10058` | `0x00000dff` |
| `0x10078` | `0x00020002` |
| `0x14000` | `0x00000006` |
| `0x14004` | `0x00000120` |
| `0x18000` | `0x00000006` |
| `0x18004` | `0x00003dff` |

Consistent with the `nac_ddr_layout_small/medium/large` / RLD surfaces of
`ce40.ko`; nothing here has been exercised.

## ILKPHY configuration (`0x1c000`, `0x20000`, `0x24000`, `0x28000`)

**Four** registers at `0x4000` stride, every one reading **`0x00000eec`** on a
fresh boot. `0x28000` is the one the vendor's `ce40_init()` writes with `0x72`
(*vendor init*, `CE40_ILKPHY_CFG`); the other three are presumably the same
register for the other links. Writing `0x72` to `0x28000` took and read back
on 2026-10-06, and **reverted to `0xeec` on the next reprogram** — so the write
is a post-boot CP initialisation step (the `ce40_init` equivalent), not part of
the FPGA load, and it is not yet wired into FFN's boot.

## SerDes lane status (`0x2c000`–`0x2ffff`, 4 × 10 live)

Eight groups of `0x800`, each one status word followed by four lane words at
`0x200` stride:

| offset in group | value |
|---|---|
| `0x000` | `0x0007c01f` |
| `0x004`, `0x204`, `0x404`, `0x604` | `0x00039052` |

32 lanes in all — the width of the Interlaken bundle toward the FE100 and the
40G links.

## Two traps in taking this dump

1. **`ffn_ca1` bounces reads at 1 KiB**, so a 4 KiB `read()` returns 1,024 bytes.
   A loop that advances by the request size samples a quarter of each page and
   reports "161 live words" that are in fact a periodic 25 % sample. Read until
   the range is complete (`measured/` holds the corrected dump).
2. **After a proper boot the CP's `/tmp` is a tmpfs.** A file written there is
   not on the MP's disk under the NFS root; pull it through `ffn-cp`, not from
   `/opt/ffn-cproot-*/tmp/`, or you will read the previous boot's file.
