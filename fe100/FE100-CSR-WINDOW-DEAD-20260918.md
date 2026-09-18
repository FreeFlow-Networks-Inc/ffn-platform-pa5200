# The FE100 register window is dead — 2026-09-18

Measured while trying to settle one question from the session-aging work: does the FE100 emit
statistics messages, and at what interval? `sem_stats_export_period_cfg` @ `0x78134` is the register
that answers it. It reads zero — and so does everything else on the chip.

**Every one of the 262,144 words in the 1 MB BAR0 reads `0x00000000`.** Not one bit set anywhere.

This is a regression, not the normal uninitialised state. The chip used to return documented reset
values:

| register | offset | read 2026-09-04 | read today |
|---|---|---|---|
| `prom_chip_rev_num` | `0xffffc` | `01 00 0a 00` (hardwired revision) | `0x00000000` |
| `nif_p0_mac_pcs_cfg` | `0x1001c` | `0x49190` (map reset value) | `0x00000000` |
| `nif_rst_ctrl` | `0x10010` | `0x000fffff` | `0x00000000` |

A hardwired revision word reading zero is not a device at reset. It is a device not answering.

## What is ruled out

Each of these was measured, not assumed.

| check | result |
|---|---|
| chip PCI `COMMAND` | `0x0142` — memory decode **on** |
| upstream bridge `0002:00:00.0` `COMMAND` | `0x0146` — memory decode and bus master on |
| bridge memory window | `0xf0000000–0xf00fffff`, **exactly covers** BAR0 |
| bridge secondary reset | 0 |
| PCIe link | trained, **2.5 GT/s ×2** (max 5.0 ×2) |
| PCI `STATUS` after sweeping all 1 MB | `0x0010` — no master abort, no target abort, no parity error |
| platform CPLD reg 4 (DP + FE100 reset) | `0x08` — reset bit 0 = **0, deasserted** |
| access method | mmap RO, mmap RW and `/dev/mem` all agree |

`sysfs enable` reads 1, but that is the kernel's enable *count*. It is not evidence the chip works,
and it was 1 throughout.

## The positive control is what makes this conclusive

Reading a device and getting zeros proves nothing on its own — the reader could be wrong, the kernel
could be mapping cacheable, the byte swap could be misapplied. So the same script read two other
devices on the same bus, same kernel, same minute:

```
FE100     0002:01:00.0   1024 KiB      0/262144 words nonzero  (0.0%)
BCM88375  0001:01:00.0     32 KiB    839/8192   words nonzero  first 0x75000000
DP        0003:03:00.0   8192 KiB   8153/262144 words nonzero
```

`0x75000000` byte-swapped is `0x8375` — the BCM answering with its own part number. The reader is
fine. The FE100 specifically is silent.

## What it means

Completions return — there are no aborts — while the entire register space reads zero. The PCIe and
PHY domain is alive; the CSR domain is not clocked.

**Nothing in this directory can fix it.** `ffn_fe100.py`, `ffn_fe100_clocks.py` and
`ffn_fe100_reset.py` all work by reading and writing CSRs, so they sit downstream of the thing that
is broken. Whatever made the chip answer during the 2026-09-09 and 2026-09-15 physical-session runs
is not running now.

Two candidates worth investigating in order:

1. **The control plane is on a different kernel** than it was for those runs.
2. **The CE FPGA is programmed by `brdagent` on the Octeon**, per the documented chainload order, so
   a clock the FE100 depends on may be gated behind a step in that chain that no longer runs.

## The one obvious lever is a trap

The platform CPLD's reg 4 bit 0 resets the DP **and** the FE100 together. Pulsing it would take down
the running 40-core dataplane. `tools/ffn_mpcpld.py --pulse` preserves every other bit, but this is
never a casual operation and should not be done to chase a register read.

## Consequence

Every FE100 item is blocked behind restoring CSR access — not only session aging, but the whole
offload path. The open question in the aging work ("is the FE100 configured to emit statistics at
all, and at what interval?") cannot be answered until the chip answers at all.

**Re-measure before planning any FE100 task.** A three-device BAR sweep takes seconds and
distinguishes "the chip is dead" from "my reader is wrong" — which no handful of individual offsets
can do.
