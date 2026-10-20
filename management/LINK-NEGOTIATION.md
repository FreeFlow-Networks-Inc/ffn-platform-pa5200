# Link negotiation per faceplate port

Every faceplate port is one kind of thing to the DP owner (`ffn-portd
physical`) and one kind of thing to the configuration (`ethernet1/N` with
link-speed, link-duplex and link-state). What differs per port is how its link
comes up, and that belongs to the CP, which owns the PHYs on the OCTEON SMI
bus, the switch MACs and the SFP cages. This note fixes the requirement for
the CP owner that `ffn-portctl`, `ffn-sfpctl`, `ffn_faceplate.py` and
`ffn_copper_link.py` merge into: detect the media and negotiation of every
port by itself, apply the committed speed through the right clause, and
report the detected mode in the faceplate observation the DP owners consume.

## Required per port

| ports | media | auto-detection the CP owner must do | negotiation |
|---|---|---|---|
| 1-4 | RJ-45 copper, BCM84848-class PHY on the OCTEON SMI bus | PHY present at its commissioned address, link partner rate 10/100/1000/10GBASE-T | IEEE 802.3 Clause 28 (10/100/1000BASE-T) and Clause 73 for 10GBASE-T where the PHY offers it; `auto` means every advertised rate |
| 5-20 | SFP/SFP+ cage | module present, SFF-8472 identity: 1G optics vs 10G optics vs direct-attach copper | 1G optics: 1000BASE-X with Clause 37 autonegotiation; 10G optics: XFI, no autonegotiation; 10G DAC: Clause 73 where both ends offer it, else forced |
| 21-24 | QSFP28 cage | module present, SFF-8636 identity: 40G/100G optics vs DAC, lane count | optics: XLAUI/CAUI-4, no autonegotiation; DAC (40GBASE-CR4 / 100GBASE-CR4): Clause 73 autonegotiation with Clause 72 / Clause 92 link training and Clause 91 RS-FEC as negotiated |

What exists today: `ffn-sfpctl` already resolves the SFP link mode from the
module's identity (1000BASE-X plus Clause 37 for 1 Gb/s optics, XFI for 10 Gb/s)
and re-links a port when a module is inserted; the copper PHY path applies the
committed rate and reports the advertised set. Neither Clause 73 autonegotiation
nor link training is driven by FFN code yet: the switch's own PHY firmware does
it when enabled through the SDK, and the CP owner must own and read back that
state the same way it owns the Clause 37 mode.

## Clauses 169 to 173

The operator asked for IEEE 802.3 Clauses 169, 170, 171, 172 and 173 as well:
the 802.3dj generation (200 Gb/s per lane signalling for 200G, 400G, 800G and
1.6T Ethernet: its PCS, PMA, AUI and backplane/copper-cable PMDs). They are
recorded here as a requirement of the unified port owner's design, with one
limit stated plainly: the PA-5220's BCM88375 and its SerDes cannot run 200 Gb/s
lanes, so on this platform those clauses can be detected and reported only as
"not supported by this port", never negotiated. They apply to the in-house
FPGA platform and to future silicon; the port owner's media table is where
that support is added, and nothing in the DP owner has to change for it.

## What the DP owner needs from all of this

Only the observation: per port, `media`, the detected `link_mode` and
`autoneg`/negotiation clause, `link`, `speed_mbps` and, for copper, the PHY
and MAC agreement. The `ffn-port-links` delivery carries the faceplate rows as
they are; the DP owner judges carrier from them and publishes the row in its
status document, so the WebUI and the CLI can show the negotiated mode of
every port from one place.
