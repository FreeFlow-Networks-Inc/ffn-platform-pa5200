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
PHY domain is alive; the CSR domain is not.

**Nothing in this directory can fix it.** `ffn_fe100.py`, `ffn_fe100_clocks.py` and
`ffn_fe100_reset.py` all work by reading and writing CSRs, so they sit downstream of the thing that
is broken.

Two candidates were worth investigating: that the control plane is on a different kernel than it was
for those runs, or that a clock the FE100 depends on is gated behind a step that no longer runs.

---

# 2026-09-20: the CE FPGA is not programmed — but that is NOT the cause

## The measurement

    CE CPLD 0x1b020000    0c ff 20 00 00 00 00 00      reg2 = 0x20   FPGA DONE (bit 0x40) CLEAR
    recorded 2026-09-02   0c ff 60 00 00 40 00 00      reg2 = 0x60   FPGA DONE        SET

DONE was set when the registers answered. It is clear now. The CPLDs sit on the Octeon's own boot
bus, so reading them touches no part of the PCIe path that is in question.

## Why that accounts for every symptom

The FE100's PCIe endpoint and its register block are in different clock domains, and only the
endpoint survives without the FPGA:

| observation | explanation |
|---|---|
| link trains, device enumerates, config space reads fine | endpoint runs off the PCIe domain |
| BAR reads return `0x00000000`, never `0xffffffff` | endpoint decodes and **claims** the cycle |
| no abort, no Unsupported Request, no error of any kind | endpoint **completes** the read normally |
| hardwired ID words read zero, not their values | the register array itself is not being driven |

That last row is what rules out "merely uninitialised". `prom_chip_rev_num` is a hardwired revision
word, and `nif_rst_ctrl` and `nif_p0_mac_pcs_cfg` have documented non-zero reset values. A block at
reset shows its reset values. A block reading zero everywhere is unclocked.

## Two controls that make it evidence rather than a story

**An unclaimed address in the same domain reads all-ones.** Domain 2's bridge forwards only
`0xf0000000–0xf00fffff`, so anything above that is unclaimed by construction:

```
bus 0xf0000000   FE100 BAR0                      00000000 00000000 ...
bus 0xf0080000   FE100 BAR0 + 512K               00000000 00000000 ...
bus 0xf0100000   just past the bridge window     ffffffff ffffffff ...
bus 0xf0300000   well past it                    ffffffff ffffffff ...
```

`0x00000000` and `0xffffffff` are different failures. The FE100 is answering.

**The reads are not being rejected.** `DEVSTA` showed `UnsupportedReqDetected`, which looked
damning — but those bits are sticky and enumeration sets them too, so it proves nothing on its own.
Clearing them, idling, then reading the BAR leaves them clear:

```
1. as found            0x000a NonFatalErr UnsupportedReq
2. after clearing      0x0000 (all clear)
3. idle, no BAR access 0x0000 (all clear)
4. read 3 BAR words    00000000 00000000 00000000
5. after BAR reads     0x0000 (all clear)
```

## Who programs it, and why nothing does now

`cpldlib_fpga_program(file, id)` is defined in **`libpancommon_cp.so`** and is **absent from the MP
build** — programming happens from the Octeon, not the host. Its caller is
`brdagent/cp/libfpga.so`, and the CP carries a dedicated `_ce40lib.so` / `ce40lib.pyc` binding. The
bitstream is `ce40.bin` (48 MB), shipped beside `ca1.bin` with an `fpga-images` manifest and
detached signature.

So the FPGA is programmed by the vendor's **brdagent, running on the Octeon control plane**. FFN's
own control plane does not run brdagent, so on an FFN boot nothing programs it.

**This was never fixed, only inherited.** The 2026-09-02 through 2026-09-15 runs found the FPGA
already loaded — left over from an earlier vendor boot, since FPGA configuration is volatile but
survives for as long as it is neither power-cycled nor reset. Once that was lost, the FE100 went
with it. The chip did not break; the thing it was borrowing went away.

## What this changes

It moves the FE100 from "mysteriously dead" to a named missing bring-up step, and it means **every
FE100 result on this appliance to date depended on a state FFN never established for itself**. Any
plan that assumes the chip answers needs to own the FPGA load first.

Not yet determined: whether `ce40.bin` can be loaded by own code through the CPLD, or whether the
signed `fpga-images` manifest gates it. Note the related finding that `ca1.bin` has no loader on
this platform — `ce40.bin` is a different bitstream and that conclusion does not transfer to it in
either direction.

---

# DISPROVEN the same day, on hardware

The section above reads as an answer. It is not. The FPGA really was unprogrammed, and that really
was a missing bring-up step — but programming it does **not** revive the FE100.

The load now runs from FFN's own boot path. The console printed `Full fpga programming SUCCESS`, and
CE CPLD reg 2 went `0x20` → `0x60`, the whole file reading `0c ff 60 00 00 40 00 00` — byte for byte
the 2026-09-02 value.

**The FE100 still reads `0x00000000` across all 262,144 words.** Stable across repeated reads, with
memory decode on, the link trained at 2.5 GT/s ×2, and the device in D0. PCI enumeration happened
*after* the FPGA load on that boot, so it is not an ordering problem either.

## Why the evidence looked conclusive and was not

DONE was set on 2026-09-02/04 when the registers answered, and clear on 2026-09-18 when they did
not. That correlation is real, and it is **coincidence**: both states tracked whether a vendor boot
had last run, and a vendor boot does a great deal more than load a bitstream. Two things moving
together, both downstream of a third.

## What survives from the section above

* An unclaimed address in domain 2 reads all-ones; the FE100 reads all-zeros. It claims and
  completes the cycle. (Independently re-confirmed since: on a boot where memory decode happened to
  be off, the same addresses read all-ones.)
* The reads are not rejected — UR does not re-latch after clearing.
* A register file reading zero rather than its documented non-zero reset values is unclocked or held
  in reset, not merely uninitialised.

## The next candidate, untried

Loading the bitstream is not the whole vendor sequence. `brdagent/cp/libfpga.so` exports `ce40_init`,
`ce40_interface_reset` and `octeon_spi_initialize` / `_interface_config` / `_do_work` — an SPI
bring-up run against the now-configured FPGA. None of that happens on an FFN boot either. It is a
different mechanism from the bitstream load, not a continuation of it, and it has not been attempted.
