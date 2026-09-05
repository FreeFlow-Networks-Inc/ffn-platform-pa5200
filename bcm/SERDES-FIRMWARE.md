# The SerDes microcontroller does not start — why no port on this chip links

**Fixed in practice; root cause narrowed but not closed. 2026-09-05.**

*Revised after review: section 3 originally argued the microcode download was
NOT corrupt. That was wrong, and the reasoning error is documented rather than
quietly deleted. The fix and the measurements were never in question.*

Every port on the BCM88375 reads `down` under our own OpenBCM 6.5.26 build. Not
just the 40G links to the dataplane — every port, including the 10G links to the
management plane that demonstrably passed frames under the vendor SDK. That is
the shape of the problem: it is **chip-wide, and it is a regression in our
build**, not a dark link to chase one port at a time.

## What is actually broken

The TSCE SerDes microcontroller never reaches "ready for command".

The PMD on each quad has an embedded microcontroller that runs downloaded
microcode. It is what performs RX adaptation — CDR, DFE, offset correction. The
transmitter is hardware and runs without it; **the receiver cannot lock without
it.** A chip whose SerDes uC is not running can transmit and can loop back
internally, and will never achieve link with anything.

That is exactly what this chip does.

## The evidence, in the order it was found

**1. `phy diag <port> dsc` times out identically on every port.**

```
rd_uc_dsc_ready_for_cmd() = 0
rd_uc_dsc_supp_info()     = 0
rd_uc_dsc_data()          = 0
Uc Core Status Byte       = 0
ERROR: SerDes err_code = ERR_CODE_POLLING_TIMEOUT
```

Register *access* is fine: a second dump shows `rd_uc_dsc_gp_uc_req() = 4`,
which is the command byte the SDK had just written, read back correctly. So the
S-channel reaches the PMD and the PMD's registers answer. What never happens is
the microcontroller responding to the command it was handed.

Crucially this fails on **`xl2`, whose link reads `up`** — see "the port that
lied" below. A diagnostic that fails on a working port is measuring something
other than the link, and here it is measuring the uC.

**2. Turning the firmware checksum back on makes init fail.**

`config.bcm` ships `load_firmware.BCM88650=0x2`. In `jer_nif.c:1326` the top
byte selects verification:

```c
SOC_DPP_JER_CONFIG(unit)->nif.fw_verify[quad] = (fw_load_method & 0xff00 ? 1 : 0);
pm4x10_info->fw_load_method &= 0xff;
```

`0x2` is method `phymodFirmwareLoadMethodExternal` with the verify bits **clear**
— the check is off. Setting `0x102` turns it on, and init then dies:

```
pm4x10.c[4148] _pm4x10_pm_serdes_core_init      Operation failed
pm4x10.c[4345] _pm4x10_pm_core_init             Operation failed
pm4x10.c[4580] _pm4x10_port_attach_resume_fw_load Operation failed
   -> jer_nif.c[3695] -> bcm_petra_port_probe -> bcm_petra_init
```

**...but this is NOT independent confirmation, and it may not even mean what it
looks like.** Added after a second review round.

`eagle_tsc_ucode_crc_verify` issues its command through
`eagle_tsc_pmd_uc_cmd_with_data`, which *begins* with
`eagle_tsc_poll_uc_dsc_ready_for_cmd_equals_1(pa, 1)` — the same
ready-for-command poll on the same register that `phy diag dsc` uses. So
evidence 1 and evidence 2 are **one measurement reported twice**, not two paths
agreeing. An earlier draft of this document claimed they were independent. They
are not.

Worse, that `1` is a timeout in **milliseconds**, and the sequence just above it
is:

```c
    eagle_uc_reset(&core_copy.access, 1);   /* release uc reset */
    /* we need to wait at least 10ms for the uc to settle */
    /* PHYMOD_USLEEP(10000); */             <-- commented out upstream
```

A healthy DW8051 is not necessarily ready 1 ms after its reset is released with
the settle delay removed. **The `0x102` failure is therefore also consistent
with a pure timing race, and on its own proves nothing.** An earlier draft
dismissed the settle-delay hypothesis with "the poll fails even when enabled, so
time alone does not fix it" — which is circular: it used evidence 2 to rule out
the very defect that could explain evidence 2.

What still carries the conclusion is evidence 1 **on its own**: `phy diag dsc`
run *minutes* after a completed init, with the microcontroller having had all
the time it could want, still read `ready_for_cmd = 0` and returned no
`UCODE_VER`, `COM_CLK` or `PLL_LOCK` at all. And after the fix, that same 1 ms
poll on that same register succeeds and returns real values. A race does not
explain a uC that is still not ready minutes later, nor one that becomes ready
purely because the microcode was delivered a different way.

**3. The microcode in PMD RAM does NOT match — the download is corrupt.**

*This section previously said the opposite, and was wrong. Corrected 2026-09-05
after review; the error and how it was made are kept below because the mistake
is instructive.*

`tsce.c` has two verification paths, selected by `TSCE_PMD_CRC_UCODE`:

```c
#ifndef TSCE_PMD_CRC_UCODE
    /* byte-for-byte read-back against the source array */
    if (eagle_tsc_ucode_load_verify(&core_copy.access, (uint8_t *) &tsce_ucode, tsce_ucode_len)) { ... }
#endif
...
    if (PHYMOD_CORE_INIT_F_FIRMWARE_LOAD_VERIFY_GET(init_config)) {
#ifndef TSCE_PMD_CRC_UCODE
        eagle_tsc_poll_uc_dsc_ready_for_cmd_equals_1(...);   /* return value DISCARDED */
#else
        PHYMOD_IF_ERR_RETURN(
                eagle_tsc_ucode_crc_verify(&core_copy.access, tsce_ucode_len, tsce_ucode_crc));
#endif
    }
```

**`TSCE_PMD_CRC_UCODE` is defined**, as `1`, at `tsce.c:65` — in the .c file
itself. So the `#ifndef` blocks are compiled OUT and the `#else` is live:

* the byte-for-byte `eagle_tsc_ucode_load_verify` **never runs**;
* what `0x102` enables is `eagle_tsc_ucode_crc_verify`, wrapped in
  `PHYMOD_IF_ERR_RETURN`, and **that** is what fails init.

So the evidence says the microcode CRC does not match. The download IS corrupt,
and the DW8051 executes garbage — which is consistent with every observation and
with `0x1` (a completely different transfer path, MDIO rather than PRAM/UCMEM)
working.

**How the wrong conclusion was reached, because the method matters:** the
original check was

    grep -rn 'TSCE_PMD_CRC_UCODE' --include=*.mk --include=Make* --include=*.h .

`--include` filters that omit `*.c` cannot find a `#define` in a `.c` file. The
grep returned nothing, "not defined" was recorded as established, and a whole
section was built on it. A search that can only confirm one answer is not
evidence for the other.

## What is NOT the cause

**Host endianness.** The External path is the only firmware route with a
host-endian byte shuffle — `_portmod_dma_buf_alloc` (portmod_common.c:434)
selects between `arr_pos_be[3][16]` and `arr_pos_le[3][16]` on an `endian`
argument that reaches it from `portmod_sys_get_endian` →
`soc_cm_get_endian` → `CMVEC(unit).big_endian_other` →
`bde->pci_bus_features()`, i.e. from **FFN's own ffn_bde**. Getting that wrong
on a big-endian host would scramble the microcode exactly as observed.

It is not wrong. Measured on the running module:

    be_pio 1   be_packet 1   be_other 1

`be_other=1` selects `arr_pos_be`, which is correct for this host. Checked
because it was a good hypothesis, and recorded because "we checked and it was
fine" is worth as much as a finding — it stops the next person re-deriving it.

That leaves the transfer itself: `data_swap`
(`portmod_ucode_buf_order_reversed`, set unconditionally for the Jericho pm4x10
path at `jer_nif.c:1301`) indexes those tables, and the UCMEM write path runs
over the S-channel through the PAXB hardware byte-swap this platform needs. One
of those two is the remaining suspect. **Not yet established** —
`eagle_tsc_ucode_crc_verify` returning failure says the bytes are wrong, not
which stage made them wrong.

## Why nobody noticed

`load_firmware=0x2` disables the only check that would have said so, and the
vendor's own comment in `config.bcm` records why they disabled it:

```
## Firmware Load Method
# PAN: This works better on our board
load_firmware.BCM88650=0x2
# What 6.4.6 originally had causes firmware load errors
#load_firmware.BCM88650=0x102
```

They hit a failure here too and silenced the check rather than resolving it.
Inheriting that setting means our build reports a clean init — `init_errors: []`
— for a chip whose receivers cannot lock.

## The port that lied

`xl2` reads `up   40G  FD  ... XLAUI 16360  PHY` and was previously recorded as
proof that the BCM<->dataplane 40G link was alive. It is not a link at all.
`config.bcm` says so directly:

```
# Port used to take the place of Hawk's Marvell Loopback Port
# (for Health Monitoring)
ucode_port_2.BCM88650=XLGE11:core_0.2
# Should be put in PHY loopback at init time
```

The last column of `ps` is loopback mode, and `xl2` is the only port on the chip
with `PHY` in it. It is up because it is wired to itself, and PHY loopback is a
digital path that does not need RX adaptation — which is precisely why it still
works while every real link is dark. It is, in hindsight, a clean confirmation
of the diagnosis rather than a counterexample to it.

## Ruled out along the way

| hypothesis | how it was ruled out |
|---|---|
| TX FIR never applied | `jer.soc:237` does run `cint phy_tx_settings.c`, and the log says `PAN: all ports tuned, FEC enabled on CAUI ports` |
| ports excluded from linkscan | linkscan bitmap `0x…edfffe` omits only ports 0, 17 and 20; 24/25/26 are all scanned |
| pause configured differently on the DP ports | real, and intentional: `gryphon_llfc.c` sets TX LLFC on a bitmap that excludes 3/24/25/26 and RX LLFC on one that includes them. Explains the `RX` vs `TX RX` column in `ps`. Not a link cause |
| ~~corrupt microcode download~~ | **NOT ruled out — this was wrong.** That verify is compiled out (`TSCE_PMD_CRC_UCODE` is defined); see section 3 |
| nothing fitted behind xl24/25/26 | **resolved**: with the fix, xl24 came up and xl25/xl26 stayed down. Port 24 is the populated one (vendor labels it DP1) |
| host endianness in the ucode DMA shuffle | measured: `be_other=1` on the running ffn_bde, which selects the big-endian table. Correct |


## The override is chip-wide, not TSCE-only

`load_firmware.BCM88650` is read at **three** places in `jer_nif.c`, and the
base key is the fallback for all of them:

| site | macro | what it covers |
|---|---|---|
| `jer_nif.c:1325` | PM4x10 / **TSCE** | the 10G and 40G macros — what this document is about |
| `jer_nif.c:1127` | PM4x25 / **TSCF** (Falcon) | the CAUI/100G macros, including `ce12` (HSCI) |
| `jer_nif.c:3453` | **fabric** | and `if (fw_load_method_fabric == phymodFirmwareLoadMethodExternal)` additionally enables a **broadcast** firmware download, which `0x1` turns off |

So `0x1` moves every SerDes macro on the die to MDIO loading and disables the
fabric broadcast optimisation. Two consequences worth writing down:

* some of the ~8.5 s extra init is the **lost broadcast download**, not just
  slow MDIO — so that number is not a clean measure of MDIO's cost;
* a future 100G or fabric-lane problem will not obviously connect to a property
  documented as a TSCE fix. It is one property with a chip-wide blast radius.

TSCF has its own `#define TSCF_PMD_CRC_UCODE 1` (`tscf.c:63`), so the same
dead-`#ifndef` confusion that produced the original wrong conclusion applies
there too.

**If the blast radius ever needs narrowing:** the property is per-quad
addressable (`load_firmware_quad<N>`, `jer_nif.c:1325`) and per-block
(`load_firmware_fabric`), so TSCE quads can be moved to MDIO while TSCF and the
fabric stay on External. Not done, because the ports currently work and changing
it costs a test cycle on a live chip.

## Status

**Fixed in practice, not at the root.** `load_firmware.BCM88650=0x1` (Internal /
MDIO load) makes the microcontroller start and the ports link; it is carried as
an FFN override in `octeon/bcmagent/ffn-bcm-overrides.conf`. The PRAM/UCMEM path
the vendor ships is still broken in our build and is avoided, not repaired.

## Where to look next

The CRC verify says the bytes that reached PMD RAM are wrong. It does not say
which stage made them wrong, and only two stages are left:

1. **`data_swap`.** `jer_nif.c:1301` sets `PORTMOD_USER_ACCESS_FW_LOAD_REVERSE`
   unconditionally for the pm4x10 path, so `_portmod_dma_buf_alloc` gets
   `portmod_ucode_buf_order_reversed`. That value indexes `arr_pos_be[3][16]`.
   Dumping the first 16 bytes it produces and comparing against `tsce_ucode`
   would settle it in one run.
2. **The UCMEM write path.** The bytes go out over the S-channel through the
   PAXB hardware byte-swap this platform needs (`BAR0+0x2030 = 0x01010101`,
   see [[ffn-bcm-paxb-byte-order]]). A swap that is right for 32-bit register
   access is not automatically right for a wide-memory write.

**The cheap decisive experiment, not yet run:** `load_firmware` is per-quad
addressable — `soc_property_suffix_num_get(unit, quad, spn_LOAD_FIRMWARE,
"quad", ...)` at `jer_nif.c:1325` — so `load_firmware_quad<N>` can put ONE quad
on the PRAM path while the rest use MDIO. That isolates the failure to a single
quad's worth of state and makes a corrupt-vs-correct comparison possible on a
live chip without taking every port down.

Two things NOT worth chasing first, and why:

* **The uC clock.** A `0x2` init completes cleanly and `phy diag dsc` shows
  `COM_CLK 156.25MHz` and `PLL_LOCK 1` once the microcode is good. The clock and
  PLL are not gated on the microcontroller running.
* **A race on the commented-out 10 ms settle.** With verify off there is neither
  a delay nor a poll after `eagle_uc_reset`, which looked suspicious. But the
  poll fails even when enabled, so time alone does not fix it.

## A methodological note

Two claims in the first version of this document were wrong, and both failed the
same way: a search that could only confirm one answer was treated as evidence
for the other. `--include` filters that excluded `.c` files "proved"
`TSCE_PMD_CRC_UCODE` undefined. The fix in both cases was cheap — run the search
that could falsify it. The empirical result (`0x1` works, `0x2` does not, ports
come up) was never in doubt; only the explanation was.
