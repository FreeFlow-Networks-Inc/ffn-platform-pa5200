# PA-5200 faceplate interface map

Recovered 2026-10-03 from our own PA-5220's PAN-OS 11.2.4-h5 disk. These are
**facts about the board** — port numbering, I2C bus assignments, BCM logical
port numbers. Sources: `/opt/dpfs/etc/PA-5200-i2c.json`,
`/opt/dpfs/usr/share/broadcom/config.bcm`, and
`/opt/dpfs/usr/share/broadcom/enable_fp_ports.c`.

## Faceplate → BCM physical port

From `enable_fp_ports.c`, which lists the 25 ports `bcm.user` must enable
(config.bcm disables all front-panel ports at startup). Array order is
faceplate order.

| faceplate | BCM port | block | core | media |
|---|---|---|---|---|
| ethernet1/1 | 28 | XE32 | core_1 | **RJ45** |
| ethernet1/2 | 13 | XE33 | core_1 | **RJ45** |
| ethernet1/3 | 14 | XE34 | core_1 | **RJ45** |
| ethernet1/4 | 15 | XE35 | core_1 | **RJ45** |
| ethernet1/5 | 16 | XE25 | core_0 | SFP+ |
| ethernet1/6 | 1 | XE24 | core_0 | SFP+ |
| ethernet1/7 | 18 | XE26 | core_0 | SFP+ |
| ethernet1/8 | 19 | XE27 | core_0 | SFP+ |
| ethernet1/9 | 6 | XE29 | core_0 | SFP+ |
| ethernet1/10 | 21 | XE31 | core_0 | SFP+ |
| ethernet1/11 | 22 | XE30 | core_0 | SFP+ |
| ethernet1/12 | 23 | XE28 | core_0 | SFP+ |
| ethernet1/13 | 7 | XE64 | core_0 | SFP+ |
| ethernet1/14 | 11 | XE65 | core_0 | SFP+ |
| ethernet1/15 | 36 | XE66 | core_0 | SFP+ |
| ethernet1/16 | 27 | XE67 | core_0 | SFP+ |
| ethernet1/17 | 10 | XE57 | core_0 | SFP+ |
| ethernet1/18 | 29 | XE56 | core_0 | SFP+ |
| ethernet1/19 | 30 | XE59 | core_0 | SFP+ |
| ethernet1/20 | 31 | XE58 | core_0 | SFP+ |
| ethernet1/21 | 32 | CGE3 | core_1 | **QSFP28** |
| ethernet1/22 | 33 | CGE5 | core_1 | **QSFP28** |
| ethernet1/23 | 34 | CGE4 | core_1 | **QSFP28** |
| ethernet1/24 | 35 | CGE2 | core_1 | **QSFP28** |
| *(HSCI)* | 12 | CGE1 | core_0 | internal |

The four RJ45 ports are all on **core_1** as a contiguous block (XE32–XE35),
consistent with them sharing one quad PHY — the Marvell 88X3340 on OCTEON SMI
bus 0, addresses 0x10–0x13.

## Faceplate → optics I2C bus

From `PA-5200-i2c.json`. **Identical for 5220 / 5250 / 5260 / 5280** — the
faceplate hardware is the same across the family; the SKUs differ in licensed
capacity, not in ports.

| faceplate | type | I2C bus |
|---|---|---|
| 5, 6, 7, 8 | SFP+ | 10, 11, 12, 13 |
| 9, 10, 11, 12 | SFP+ | 15, 14, 17, 16 |
| 13, 14, 15, 16 | SFP+ | 22, 23, 24, 25 |
| 17, 18, 19, 20 | SFP+ | 19, 18, 21, 20 |
| 21, 22, 23, 24 | QSFP28 | 5, 4, 7, 6 |

Ports 1–4 (RJ45) have no entry — copper, no optics module to talk to.

Note the **pairwise swaps**: 9/10→15/14, 11/12→17/16, 17/18→19/18, 19/20→21/20,
21/22→5/4, 23/24→7/6. A PCB routing artifact — do not assume bus = port + k.

## The non-faceplate BCM ports

The same `config.bcm` `ucode_port` table assigns everything else:

| BCM port | block | role | header in/out |
|---|---|---|---|
| 0 | CPU | host CPU port | — |
| 3 | CGE0 | **→ Sesto gearbox → FE100** | ETH / DSA_RAW |
| 4, 5 | XE36, XE37 | **CP KR** | TM / TM |
| 8, 9 | XE38, XE39 | **MP KR** | TM / RAW |
| 12 | CGE1 | **HSCI** | TM / RAW |
| 17 | RCY | **recycle** | TM / RAW |
| 20 | ILKN4 | **→ FE100 (Interlaken)** | TM / TM |
| 24, 25, 26 | XLGE17, XLGE13, XLGE10 | **DP0–DP2, 40G** | TM / RAW |
| 2 | XLGE11 | unassigned here | — |

So the BCM has two paths to the FE100 (port 3 via the gearbox, port 20 via
Interlaken), three 40G links to the dataplane Octeon, and separate KR links to
the control and management planes.

## Caveats

- Array order in `enable_fp_ports.c` is *assumed* to be faceplate order
  1..24. It is consistent with the media grouping (4 copper first, 4 QSFP last)
  and with the I2C map, but the file itself does not label the ports.
- `enable_fp_ports.c` is a **diagnostic** script: config.bcm deliberately
  disables all front-panel ports at `bcm.user` startup, and this re-enables
  them. Its presence does not mean ports come up by default.
