# The SerDes microcontroller does not start — why no port on this chip links

**Status: root cause identified, not yet fixed. 2026-09-05.**

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

**3. The microcode bytes in RAM are correct — so it is not a corrupt download.**

This was the first hypothesis and it is wrong, which matters because it is the
obvious one. In `tsce.c:3337`:

```c
#ifndef TSCE_PMD_CRC_UCODE
    /* next we need to check if the load is correct or not */
    if (eagle_tsc_ucode_load_verify(&core_copy.access, (uint8_t *) &tsce_ucode, tsce_ucode_len)) {
        ... return PHYMOD_E_INIT;
    }
#endif
```

`TSCE_PMD_CRC_UCODE` is defined nowhere in the tree, so that byte-for-byte
read-back comparison runs on **every** init — including the ones that succeed.
The image in PMD RAM matches `tsce_ucode` exactly.

What the verify *flag* adds, a few lines later, is the only other thing:

```c
    eagle_uc_active_set(&core_copy.access, 1);   /* uc active */
    eagle_uc_reset(&core_copy.access, 1);        /* release uc reset */
    /* we need to wait at least 10ms for the uc to settle */
    /* PHYMOD_USLEEP(10000); */                  <-- commented out upstream
    if (PHYMOD_CORE_INIT_F_FIRMWARE_LOAD_VERIFY_GET(init_config)) {
        eagle_tsc_poll_uc_dsc_ready_for_cmd_equals_1(&phy_access_copy.access, 1);
    }
```

So: **correct microcode, uC released from reset, and it never becomes ready.**
The poll that fails here is the same one that times out in `phy diag dsc`. Two
independent paths agree.

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
| corrupt microcode download | `eagle_tsc_ucode_load_verify` runs unconditionally and passes |
| nothing fitted behind xl24/25/26 | not yet ruled out, but cannot explain ports 8/9, which linked under the vendor build |

## Where to look next

The uC is released but does not run, on every quad. In order of likelihood:

1. **uC clock.** The `dsc` header prints `COM_CLK` and `PLL_LOCK` columns and we
   have never read a value for either. If the PMD common clock is not running,
   nothing else matters.
2. **`eagle_uc_active_set` / `eagle_uc_reset` not landing.** These are ordinary
   register writes, but this platform already needed a byte-order fix
   (`BAR0+0x2030 = 0x01010101`) before the S-channel worked at all. A write that
   reads back correctly is not proof the *field* landed where the PMD expects it.
3. **The commented-out 10 ms settle.** Upstream removed the delay and left the
   poll to cover it — but with verify off there is neither a delay nor a poll,
   and core init proceeds to lane map, polarity and PLL config while the uC is
   still booting. This cannot be the whole story (the poll fails even when
   enabled, so time alone does not fix it) but it may compound it.

Measure before changing anything: the useful next reading is the uC control and
clock registers directly, not another init.
