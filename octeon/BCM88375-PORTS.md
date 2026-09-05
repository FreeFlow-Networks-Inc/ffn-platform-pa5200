# BCM88375 port map, and why the front panel is dark

Facts about how the PA-5220's switch is wired and configured. Recovered by
reading the board's own Broadcom SDK configuration on the appliance
(`/usr/share/broadcom/`), which is vendor content used in place and never
packaged — see the project's no-redistribution rule. What is recorded here is
the wiring and the configured state: which physical port is which interface,
which front-panel connector it serves, and which ports the shipped config
switches off. No vendor code is reproduced.

## The switch answers, and that much is verified

`ffn_bcm.ko` binds the CMIC and reads the device back:

    ffn_bcm 0001:01:00.0: BCM88375 CMIC bound: bar2 8388608 bytes,
                          ident 0x00008375, /dev/ffn_bcm

    pci    0001:01:00.0        device 14e4:8375 rev 11
    bar0   0x00011c0101800000     32768 bytes
    bar2   0x00011c0100800000   8388608 bytes   (CMIC)
    schan  0 ops, 0 errs, 0 timeouts
    ledup0_ctrl 0x00000000  LEDUP_EN=off        ledup0_clkdiv 0x64

`ledup0_clkdiv` reading 0x64 rather than 0 or all-ones is the useful part: it
says the CMIC window is genuinely mapped, not answering from a dead aperture.

## Port map

Property suffix in `config.bcm` is `.BCM88650` — the Jericho-family base ID, not
`.BCM88375`. Worth knowing, because grepping for the part number finds nothing.

| phys | interface | core | role |
|---|---|---|---|
| 1, 6, 10, 11, 13, 14, 15, 16, 18, 19, 21, 22, 23, 27, 28, 29, 30, 31, 36, 7 | XE24–XE35, XE56–XE59, XE64–XE67 | 0 and 1 | **20 x 10G front panel** — the 16 SFP+ and 4 RJ45 |
| 12 | **CGE1** | 0 | **HSCI** — labelled so in the vendor's own enable script |
| 32, 33, 34, 35 | CGE3, CGE5, CGE4, CGE2 | 1 | 100G-class; one serves the front QSFP28 |
| 3 | CGE0 | 1 | 100G-class → **a GEARBOX** (board.soc quad 0); not in the enable set |
| **2, 24, 25, 26** | **XLGE11, XLGE17, XLGE13, XLGE10** | 0 | **40G internal links to the DP Octeons** |
| 0 | CPU | 0 | the CMIC CPU port |
| 17 | RCY | 0 | recycle |
| 20 | ILKN4 | 0 | Interlaken **NIF on 12 fabric lanes** — see the correction below |
| 4, 5, 8, 9 | XE36–XE39 | 0 | 10G, not in the front-panel enable set |

20 XE lines up exactly with 16 SFP+ plus 4 RJ45, and the XLGE ports being the
internal DP links independently confirms what the chassis notes already said.

## Why nothing arrives at the DP

Two separate reasons, and both have to be fixed:

**1. The shipped config disables every front port and the HSCI.** `config.bcm`
carries `port_init_speed_<N>=-1` for exactly the 25 ports in the vendor's
`enable_fp_ports.c` list — the 20 front XE ports plus CGE1/CGE2/CGE3/CGE4/CGE5.
`-1` means "do not initialise". Grepping for any `port_init_speed_<N>=` that is
**not** `-1` returns nothing at all.

So the front panel being dark is the vendor's shipped default, not a fault. PAN's
own control plane turns the ports on after its init; that is the layer FFN has to
replace. The vendor ships a diagnostic cint script whose comment says as much
outright.

Note `port_init_speed_12=-1` — **the HSCI is disabled too**, which is the direct
reason DP `eth0` shows `rx_packets=0` while `carrier=1`.

**2. The device has never been initialised at all.** `schan 0 ops` on a
freshly-bound driver means no S-channel transaction has ever been issued this
boot. Without DNX device init the switch forwards nothing regardless of what any
individual port's config says.

The XLGE ports facing the DP are **not** disabled — they have no
`port_init_speed` entry, so they take the default. That is consistent with DP
`eth0` having carrier: the Octeon side of that internal link is up. It has simply
never had anything to receive.

## What the DP side currently looks like

    eth0   carrier=1  operstate=up  driver=ethernet-mac-pki
           tx_packets=94  rx_packets=0  speed=0

    dmesg: BGX nexuses 11800e0/e1/e2/e3 probed
           BGX2 created 4 PKI ports (0..3)

Four BGX2 LMACs probed but only one netdev exists. `speed=0` alongside
`carrier=1` suggests the LMAC has SerDes/MAC up with no negotiated rate, which
is what a fixed switch-to-switch link looks like when nothing has told the MAC
its speed.

The device tree here is the board's own, read from u-boot at runtime via
`ffn_fdt=`, never shipped.

## Order of work

1. DNX device init, so the switch forwards at all. Without it, per-port state is
   moot.
2. Enable the HSCI (port 12 / CGE1) — nothing reaches the Octeon until then.
3. Enable the 20 front XE ports.
4. On the DP: get the remaining three BGX2 LMACs to present as netdevs, and
   establish why `speed=0`.

Steps 2 and 3 are one operation per port in the vendor's model
(`bcm_port_enable_set`). What that costs in S-channel terms is the open
question, and the reason `ffn_bcm` exists.

---

# DNX init: where it actually lives

## The dispatch chain, for BCM88375 specifically

    rc.soc  --(BCM88375_A0/_B0)-->  jer.soc     [rc.soc's own 138 register
                                                 writes are for other chip
                                                 families and never execute here]
    jer.soc sets QMX=1 for this part, so every QMX-gated branch runs:
        rcload gryphon_dram_tune.soc     248 config properties
        rcload bcm88375_board.soc        324 config properties
        LED microcode: led 0/1/2 prog; led auto on; led 0/1/2 start
        linkscan swportbitmap=0xffff...edfffe
        setreg ILKN_SLE_{TX,RX}_{CFG,CAL_INBAND,BURST}
        cint phy_tx_settings.c / gryphon_llfc.c / panEgrTcMap.c
        config save -> runningConfig.soc

## The finding that matters: the scripts are not the init

`bcm88375_board.soc` is **324 `config` statements and nothing else** — no
procedural commands, no register writes. It is a board *property* file, not an
init sequence. Same for `gryphon_dram_tune.soc` (248 properties).

Properties are **inputs** to the SDK's compiled init. SerDes bring-up, OCB/DRAM
init, fabric setup and port creation all happen inside the SDK binary, not in
these files. So DNX init cannot be obtained by reading the `.soc` set, however
long it is.

What the scripts genuinely contain, in text:

  * three 82-byte LED microcode programs (8 x `02 <port> 67 35` per-port status
    reads, then a 42-byte tail identical across all three units)
  * the linkscan port bitmap
  * six ILKN SerDes/link `setreg` values — likely inert here, since CN73XX has
    no Interlaken
  * pointers to three PAN-authored cint scripts, which carry no Broadcom header

The LED microcode is the only real device programming available as text. It is
not reproduced in this repository: it is Broadcom-authored content from a
Broadcom-headered file, so it is read from the appliance in place like every
other piece of vendor firmware, never packaged.

## LEDUP register map, observed on live silicon

Read through `ffn_bcm` + `ffn_bcmctl` — FFN's own driver and tool, no vendor
software involved. Offsets are BAR2 (CMIC):

| offset | register | value at rest |
|---|---|---|
| `0x20000` | `LEDUP0_CTRL` | `0x00000000` — processor **disabled** |
| `0x20050` | `LEDUP0_CLK_PARAMS` | `0x005b8d80` (6,000,000) |
| `0x2005c` | `LEDUP0_CLK_DIV` | `0x00000064` (100) |
| `0x20400` | `LEDUP0_DATA_RAM` base | non-zero, unstructured |
| `0x20800` | `LEDUP0_PROGRAM_RAM` base | non-zero, unstructured |

`PROGRAM_RAM` holding non-zero bytes that do **not** match any of the three
microcode programs, while `CTRL` reads 0, is what uninitialised SRAM looks like —
not evidence that microcode was previously loaded.

`CLK_PARAMS` and `CLK_DIV` reading round, sensible values is useful the other
way: it is further confirmation the CMIC window is genuinely mapped rather than
answering from a dead aperture.

## Consequence for the plan

Loading the LED microcode and enabling the processor would be the first genuine
FFN **write** to this device, and it is visible on the front panel — a good
proof of the write path, and low risk because the LED processor is independent of
forwarding.

It does **not** make traffic flow. That still needs DNX init, which is in the SDK
binary. The two honest routes to it remain: run the vendor stack in place on the
appliance and capture what it does to the hardware, or drive the device
independently and implement from observed behaviour. Reading further `.soc` files
will not produce it.

---

## Which CPU programs this switch

The BCM88375 is on the **CP** Octeon's PCIe (`0001:01:00.0` and `.1`). Vendor ID
`14e4` appears nowhere else in the system, so the DP Octeon cannot see it: there
is no driver to load on the DP and nothing for one to bind to. The daemon that
enables and programs the front-panel ports runs on the CP.

One naming trap worth knowing before reading any of the bring-up scripts:
`/opt/dpfs` on the x86 MP is **not** the DP's filesystem. It is NFS-exported to
the PCIe-only `127.1.0.0/16` network and the **CP** roots from it, so the MP's
`/opt/dpfs/usr/share/broadcom/` and the CP's `/usr/share/broadcom/` are the same
files. The DP has a separate rootfs of its own.

The CMIC control interface — the verified register map, the per-CMC S-channel
layout, the FSCHAN direct-register path, and what is still needed to encode an
S-channel operation — is documented in the interop repository rather than here,
since it is reverse-engineering detail rather than platform wiring.

The owner-supplied DNX files this switch needs in order to initialise are
verified and loaded by `ffn_dnx.py` in FFN-NGFW; they are never shipped.


## The LED processor: what is in it, and why it is dark (2026-09-04)

`ledup0_ctrl = 0` above is not a value waiting for the right bit. **CTRL will not
accept the enable bit at all** -- `leden 0 1` leaves it 0 -- because the block is
held in reset.

### The gate is the CPS reset, which is also what stops soc_dpp_init

`soc_dcmn_cmic_device_hard_reset` writes 1 to `CMIC_CPS_RESET` (**0x10220** bit
0), sleeps 1 ms, then polls for the bit to **self-clear to 0**, giving up after
100 ms with "CPS reset field not asserted correctly". That error therefore means
the bit never read back 0 -- not that the write was refused.

Read through our own PCIe path the register is healthy:

    bar2+0x10220 = 0x00000000    CPS_RESET: not asserted, clean
    bar2+0x10224 = 0x75831100    device ID + rev, byte-reversed
                                 -> 00 11 83 75 = dev 0x8375 rev 0x11

A correct device ID at its documented offset proves that path reads real
hardware. The SDK's did not: it misclassified the device as I2C (134
`sal_i2c_init_fd` failures), so its poll of CPS_RESET never saw 0 and init died,
leaving LEDUP in reset. **The dark front panel and the stalled SDK init are one
blocker, not two**, and `-D__DUNE_LINUX_BCM_CPU_PCIE__` addresses both.

The byte reversal is also a measurement to settle the open `SYS_BE_PIO` question
against, rather than guessing at it.

### Register layout, and a hazard that costs a CP reboot

    LEDUP0 CTRL   bar2+0x20000   bit 0 = LEDUP_EN
    CLK_DIV       bar2+0x20050 / 0x2005c
    DATA_RAM      bar2+0x20400   ONLY 64 dwords -- see the hazard
    PROGRAM_RAM   bar2+0x20800

Byte-wide registers read as dwords put the byte in the **MSB** (`0x02000000` is
byte 0x02).

**HAZARD.** The DATA_RAM aperture is only `0x20400`-`0x204FC`. Reading past it
aborts on the bus even though the offset is far inside the 8 MB BAR2: a read at
`0x20780` Oopsed the kernel inside `ffn_bcm_ioctl` (`Code: <8c420000>` =
`lw v0,0(v0)`), left four **unkillable D-state** tasks pinning the module
refcount at 4 so `rmmod` refused, and cost a CP reboot. BAR size is not decode.
The aperture does not linearly cover all 256 LED-RAM bytes, so LED-RAM byte 0xa0
is **not** at `bar2+0x20680`. The full RAM needs the indexed address/data
register pair CMIC RAMs normally use; find it in the SDK before poking further.

`ffn_bcm_ioctl` should either whitelist the decoded windows or install a MIPS
bus-error fixup around the access and return -EIO, which is what Broadcom's own
BDE does. Until then, probe any new offset from a throwaway process, never from
a session holding state worth keeping.

### The resident program, disassembled

`tools/led/tools` in the SDK builds three **host** dev tools with `make all`:
`ledasm`, `leddasm`, `ledsim`, plus `tools/led/example/*.asm`. They append the
extension themselves (`./ledasm /tmp/x`, not `/tmp/x.asm`); `.hex` is 16
space-separated uppercase bytes per line. That is the own-code path for the LEDs:
understand the hardware with Broadcom's host tools, then write FFN's own `.asm`
-- needed regardless, since a chip reset wipes whatever is resident now.

    ld a,0x15 / call 0x35  x8        eight LED slots
    inc (0xe0)                       blink timebase in DATA_RAM
    send 0x08                        shift 8 bits to the external LED chain
    port a / pushst 0 / pushst 1 / tor   OR the port's two hw status bits
    and b,0x06                       blink phase from the counter
    ld b,0xa0 / add b,a / ld b,(b)   sw per-port state at DATA_RAM[0xa0+port]
    pushst 0x0e | 0x0f / pack        on-pattern vs off-pattern

**DATA_RAM split:** LED-RAM bytes 0..49 are 25 ports x 2 bytes maintained by
HARDWARE -- and 50/2 = 25 matches the faceplate count reached independently,
which corroborates the layout rather than assuming it. `0xa0` and `0xe0` are
software scratch, outside the readable aperture.

The eight slots name ports 21,1,13,1,9,1,17,5. **That is not this chassis's
faceplate map** -- 5 and 9 sit in the "not in the front-panel enable set" row of
the table above -- so it is a `generic8led`-derived leftover for another variant.
Use the table, not the LED program, for the port map.

The program is a close relative of Broadcom's `generic8led.asm` (both carry
`16 E0 CA`, `12 A0 F8`, `32 00 .. 97 75`, `77 64`), which independently confirms
the MSB-of-dword byte extraction. The microcode bytes are vendor firmware and are
deliberately NOT in this repo: bring-your-own, use-in-place, never packaged.


## The front panel is no longer dark: full init, LEDs running, ports enumerated

Our own OpenBCM build now initialises the chip end to end. `jer.soc` runs to
completion and writes `runningConfig.soc`; `Init SOC Done` and `Init BCM Done`
both appear; 41 init stages including real DDR SHMOO training, PP init, and every
BCM module (l2, l3, vlan, mpls, vswitch, cosq, fabric, linkscan, field, policer,
mirror, trunk).

### Three things had to be fixed, in this order

1. **`SYS_BE_OTHER=0`** -- see `octeon/kctl/FFN-BDE.md`. ffn_bde enables the PAXB
   *hardware* byte swap, so software must not swap DMA again. Without this,
   `soc_jer_regs_blocks_access_check_dma` failed its own data compare.

2. **KAPS off.** `jer_kaps_init_app()` hits our deliberate 49-function stub.
   `JER_KAPS_ENABLE` is driven by three sizes (jer_pp_kaps.h:39-42) whose
   config.bcm properties drop the `kaps_` prefix:
   `private_ip_frwrd_table_size=0` and `public_ip_frwrd_table_size=0` disable it.
   Appropriate here -- KAPS is an external-TCAM route engine we have no library
   for, and this chip is a NIF: the OCTEON forwards. `NoElkDevice=1
   NoElkAppl=1` on the INIT_DNX line does NOT cover the KAPS *app*.

3. **`ln -s /tmp/bcmcfg /usr/share/broadcom`**, created in the NFS root at
   `/opt/ffn-cproot-owrt/usr/share/` so it survives a CP reboot. jer.soc sources
   `phy_tx_settings.c`, `gryphon_llfc.c`, `panEgrTcMap.c` and writes
   `runningConfig.soc` by ABSOLUTE path, ignoring `BCM_CONFIG_FILE` -- the trap
   `tools/ffn-bcm-sdk.sh` already documented.

### LEDs: running

`jer.soc` enables them itself once init completes -- there was never a register
to find. **Three** LED processors, 8 slots each:

    if $?feature_led_proc && !$?simulator         "led 0 prog $ledcode_0; led 1 prog $ledcode_1; led 2 prog $ledcode_2;          led auto on; led 0 start; led 1 start; led 2 start"

`feature_led_proc` needs nothing set: `system.c:1087` resolves any `feature_*`
shell variable from the SDK feature flags, and `soc_features_bcm88375_b0` grants
`soc_feature_led_proc`. Confirmed by register, remembering the byte reversal:

    CTRL bar2+0x20000 = 0x0b020000 -> logical 0x0000020b, bit0 LEDUP_EN = 1
    (0x00000000 before init -> 0x0a020000 mid-init, bit0=0 -> 0x0b020000 now)
    CLK_DIV 0x20050 = 0x00974901   (was 0x808d5b00; SDK reprogrammed it)

`led prog` and `led start` print NOTHING on success, so an empty log is not
evidence they were skipped -- read CTRL bit 0. Physical illumination still wants
eyes on the chassis; with ports disabled, expect link-down patterns.

### The live port table (`ps` at the BCM.0> prompt)

    xe1(1)   !ena 10G   XFI      xe18(18)   !ena 10G   XFI
    xl2(2)   UP   40G   XLAUI    il20(20)   down 12.5G ILKN
    ce3(3)   down 100G  CAUI     xe21-23    !ena 10G   XFI
    xe4,5    down 10G   XFI      xl24,25,26 down 40G   XLAUI
    xe6,7    !ena 10G   XFI      xe27-31    !ena 10G   XFI
    xe8,9    down  -    KR       ce32-35    !ena 100G  CAUI
    xe10-16  !ena 10G   XFI      xe36(36)   !ena 10G   XFI
    ce12(12) !ena 100G  CAUI

`xe`=10G, `xl`=40G, `ce`=100G, `il`=ILKN. **`xl2` is UP at 40G XLAUI** -- the
live link to the DP OCTEON, now read from the chip rather than inferred.

Every front port reads **`!ena`**, which independently confirms the "shipped
config disables every front port" row in the table above, and is exactly the
state `bcm_port_enable_set` changes. The table also corroborates the faceplate
map: the 20x10G xe ports are 1, 6, 7, 10, 11, 13-16, 18, 19, 21-23, 27-31, 36;
port 12 is ce12 (HSCI); 32-35 are the 100G class.

A full init takes about **700 s** to reach the prompt, most of it SHMOO, so use
an 800 s deadline / 700 s settle on the pty runner.


## Loopback test with plugs fitted: the die is fine, the cage is not

Physical loopbacks on the SFP+ cages, the QSFP28 and the RJ45s. All 25 front
ports enabled with `cint /usr/share/broadcom/enable_fp_ports.c` (which flips
every one from `!ena` to `down` -- port control confirmed). **Nothing linked**,
and it is not stale reporting: `bcm_port_link_status_get` returns
`rv 0, link 0` for all 25, agreeing with `ps`.

`port <p> lb=` isolates it in three commands:

    ps xe13               down                external loopback: no link
    port xe13 lb=mac  ->  up  ... MAC         MAC + PCS work
    port xe13 lb=phy  ->  up  ... PHY         SerDes works
    port xe13 lb=none ->  down                external again: no link

PHY loopback links, exercising the SerDes datapath, PCS, MAC and link detection.
So **port enable, MAC, PCS, SerDes and link reporting are all good**, and the
break is strictly outside the die: SerDes pins -> cage -> module -> back.

That points at the front-panel module control plane, which FFN does not drive
yet: SFP **TX_DISABLE** and cage power are CPLD/GPIO on this chassis and are
normally de-asserted by the vendor's board agent. The related gap is I2C --
**`CONFIG_I2C_CHARDEV is not set`** in the CP kernel, so there are no
`/dev/i2c-*` nodes and the SDK's `sal_i2c_init_fd` fails, even though the buses
work in-kernel (`CONFIG_I2C=y`, `CONFIG_I2C_OCTEON=y`, EEPROM detected at
`1-0057`). Enabling I2C_CHARDEV is one kernel line; the MP CPLD at 0x600 is the
other half. Do not expect I2C alone to fix it -- on most designs TX_DISABLE is a
pin, not an I2C register.

**`linkscan` is working**, despite its shell display reading
`Software Port BitMap 0x000...0` -- it tracked every `lb=` transition within the
250 ms poll. That display is cosmetic on this chip family; do not build theories
on it (I did, twice).


---

# Corrections, 2026-09-05

Three claims above were wrong or incomplete. All three were settled by reading the vendor files
on the CP (`/usr/share/broadcom`, used in place and never packaged), not by inference.

## ILKN4 is a fabric NIF, not an external-lookup link

"not usable here, CN73XX has no ILK" reasoned from the wrong premise. `config.bcm` says

    use_fabric_links_for_ilkn_nif_ilkn4.BCM88650=1
    ilkn_num_lanes_4.BCM88650=12
    ilkn_lanes_20=0x000fff

so ILKN4 is a **network interface on 12 FABRIC lanes**, not a look-aside link to an external
lookup engine. `custom_feature_kbp_recovery_enable=0` and the absence of any ELK/KBP property
confirm no lookup device is fitted, but that was never what ILKN4 was for here. Whether it is
*usable* is still open: no `bcm88375_board.soc` quad comment maps any connector to it, and it is
absent from `enable_fp_ports.c`.

## CGE0 (port 3) goes to a Gearbox

`bcm88375_board.soc` names it four times: "CGE0 (quad 0, to Gearbox)". That also explains the
one oddity in the table — port 3 is the ONLY `in=ETH` port, which is exactly what you would
expect if whatever sits behind the gearbox speaks plain Ethernet rather than Broadcom TM.
What is behind it is still unknown; the RJ45s are on quad 8, so it is not those.

## HSCI is 100G Ethernet on CGE1 — not Interlaken, not pure L1

Both alternative readings are refuted by the vendor's own code:

* `phy_tx_settings.c` has `hsci_port_list[] = { 12 }` — exactly one HSCI port, and ILKN (port 20)
  is in no such list.
* `serdes_if_type_12=CAUI`, `phy_an_fec_12=1`, and `hsci_settings[]` enabling
  `FORWARD_ERROR_CORRECTION_CL91`. CAUI and Clause 91 RS-FEC are 100G **Ethernet**; Interlaken
  uses neither.
* `tm_port_header_type_in_12=TM`. A pure L1 link has no header type, because nothing parses it.

The useful fact underneath: `in=TM` means ingress traffic must already carry a Broadcom TM
header, so the peer has to be another chassis that knows the format — not an arbitrary Ethernet
neighbour. That is the shape of the CP/MP/DP internal links, not of the `RAW` faceplate data
ports, and it is why HSCI belongs to the device's HA configuration rather than to the security
policy.

## Complete quad map, from board.soc's own comments

| quad | goes to | | quad | goes to |
|---|---|---|---|---|
| 0 | **Gearbox** (CGE0) | | 9 | Cavium + Broadwell (XE36-39) |
| 1 | **HSCI** (CGE1) | | 10 | DP2 (XLGE10) |
| 2 | front 23 QSFP28 | | 11, 12 | **N/C** |
| 3 | front 22 QSFP28 | | 13 | DP1 (XLGE13) |
| 4 | front 24 QSFP28 | | 14 | SFP+ (see the 13-16/17-20 conflict) |
| 5 | front 21 QSFP28 | | 15 | **N/C** |
| 6 | front 5-8 SFP+ | | 16 | SFP+ (see the same conflict) |
| 7 | front 9-12 SFP+ | | 17 | DP0 (XLGE17) |
| 8 | front 1-4 RJ45 | | | |

Note quads 2-5 here are board.soc's *polarity* comments, which disagree with `config.bcm`'s
declaration order on which CGE serves which QSFP28 cage. That conflict is recorded in
`ffn_bcmports.QSFP_CONFLICT` and is still unresolved — measure it, do not guess.
