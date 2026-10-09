# SFP link mode follows the module

Faceplate ports 5 through 20 are SFP+ cages behind the BCM88375's internal
SerDes. The switch does not know what is plugged into a cage: an XFI port
fed by 1 Gb/s optics never links, and a port left in a 1 Gb/s mode after a
10 Gb/s module replaces the 1 Gb/s one never links either. The configuration
applier programs each port's link speed at commit time, so the mode has to be
chosen from the module present and re-chosen when the module changes.

## What the module says

`ffn_sfp_control.module_speeds(identity)` reads the SFF-8472 identity page
the faceplate already reads for diagnostics: the 10GBASE-ER/LRM/LR/SR codes
(byte 3), the 1000BASE-SX/LX/CX/T codes (byte 6) and, for EEPROMs that set no
code, the nominal signalling rate (byte 12, or byte 66 when 12 reads 255). A
module with an LC or SC connector and a declared wavelength is optics;
an RJ45 SFP keeps the copper/SGMII path. The live example is the
1000BASE-LX module in port 5 (OEM GLC-LH-SMD, 1310 nm, 1.3 GBd): it declares
1000 only, so 10000 is refused for it.

## Which mode the switch gets

`link_mode(identity, speed)` resolves the committed `link-speed`:

| requested | 1 Gb/s optics | 10 Gb/s optics | dual-rate optics |
|---|---|---|---|
| `auto` | 1000BASE-X, Clause 37 on | XFI, autoneg off | the higher rate |
| `1000` | 1000BASE-X, Clause 37 on | refused | 1000BASE-X, Clause 37 on |
| `10000` | refused | XFI, autoneg off | XFI, autoneg off |

Clause 37 negotiates duplex, pause and remote fault, never speed, so a fixed
1000 and auto are the same mode on 1 Gb/s optics; a partner that runs Clause
37 only links when both ends send configuration words. 10GBASE-R has no
autonegotiation, so 10 Gb/s optics run the port's native XFI with it off (the
SDK accepts autoneg on an XFI port but then reports speed 0).

`configure_fiber` applies the mode through the serialized BCM CINT endpoint:
MAC disabled, autoneg off, interface set, speed set, the Clause 37 mode
control requested, autoneg on for 1 Gb/s, MAC re-enabled, then interface,
autoneg and speed read back. The autonegotiation-mode PHY control returns
E_PARAM on this chip's internal SerDes; Clause 37 is what the SDK selects for
a 1000BASE-X interface with autonegotiation on, and the readback verifies the
interface, the autonegotiation state and the speed, not that control. A
module that cannot be classified leaves the generic SDK path in place.

`ffn_bcm_link.status` reports the interface (`link_mode`) and treats
1000BASE-X with autonegotiation as a configured 1000, so the applier's
`link-speed 1000` reads back as what it set and is not reapplied on every
commit. The faceplate's readback after a speed change expects the
module-resolved rate.

## Insertion

`ffn-sfp-watch.service` (`ffn_sfp_watch.py`) runs on the CP and polls cage
presence on the PCA9555 expander every two seconds. When a module appears in
a port that is MAC-enabled with a configured speed, it waits for the module's
identity page to validate (a fresh module takes a moment; it gives up after
30 seconds), then re-applies the configured speed through
`ffn_faceplate.relink` under the faceplate lock, so the mode follows the
module present now at the speed the configuration holds at that moment. It
never changes configuration, never acts on modules already present when it
starts (the applier owns those at commit), and leaves ports with a pending
faceplate operation or a running aggregate owner alone. Every action is one
JSON line in its journal.

## Measured on the PA-5220 (2026-10-08)

Port 5 (BCM 16) carried a Verizon handoff. With the port in SGMII, autoneg
off, speed 1000 after the generic path, our MAC reported link up (the port
syncs on the partner's idles) while the partner saw no link: our transmitter
sent idles but no Clause 37 configuration words. Applying the 1000BASE-X mode
with Clause 37 is what this change does for that port.
