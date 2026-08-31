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
| 3 | CGE0 | 1 | 100G-class, unused in the enable set |
| **2, 24, 25, 26** | **XLGE11, XLGE17, XLGE13, XLGE10** | 0 | **40G internal links to the DP Octeons** |
| 0 | CPU | 0 | the CMIC CPU port |
| 17 | RCY | 0 | recycle |
| 20 | ILKN4 | 0 | Interlaken — not usable here, CN73XX has no ILK |
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
