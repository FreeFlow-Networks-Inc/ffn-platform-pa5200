# Control-plane I2C

**Working as of 2026-09-06.** Both OCTEON TWSI buses are reachable from
userspace, without rebuilding or rebooting the control plane.

## What was actually missing

Less than it looked. The device tree already describes both controllers —
`soc@0/i2c@1180000001000` and `soc@0/i2c@1180000001200` — the in-kernel driver
already binds them (`CONFIG_I2C_OCTEON=y`, matching
`cavium,octeon-7890-twsi`), and both adapters were already registered as `i2c-0`
and `i2c-1` with a board EEPROM instantiated at `1-0057`.

The only gap was **`CONFIG_I2C_CHARDEV is not set`**, so there were no
`/dev/i2c-N` nodes and nothing in userspace could reach a working bus.

## The fix: a module, not a kernel rebuild

`i2c-dev` is an ordinary module. Setting `CONFIG_I2C_CHARDEV=m` in the control
plane's kernel tree and building just that one module produces a `.ko` with the
right vermagic (`6.18.49-dirty`, matching the running kernel), which loads into
the live system:

```
insmod /root/i2c-dev.ko          -> rc=0
/dev/i2c-0  /dev/i2c-1           -> appear immediately
dmesg: i2c_dev: i2c /dev entries driver
```

No reboot. That matters more than it sounds: rebooting the control plane takes
down the switch daemon, the dataplane and its PCIe transport with it.

Build it with `~/kbcp.sh modules` on the RE VM — the CP tree, **not** the DP
tree. Mixing them produces a module with the wrong vermagic and an hour of
"invalid module format".

## Verified with readings that check themselves

An address ACK only says something is there. These say the bus works:

```
temperature sensors (LM75-family, bus 0)
  0x48 = 51.5 C    0x49 = 50.0 C    0x4a = 47.0 C    0x4b = 46.5 C

EEPROMs 0x52-0x55, first bytes
  23 10 0c 01 84 19 00 08 ...      <- DDR4 SPD (0x23 header, 0x0c = DDR4)
```

Four plausible die temperatures and four DIMM SPDs. Nothing about that is
ambiguous.

## Topology

**Bus 0 — board management.** `0x1a-0x1f`, `0x2a`, `0x30`, `0x31`,
`0x34-0x36`, `0x48-0x4b` (temperature), `0x52-0x55` (DIMM SPD), `0x57` (board
EEPROM, claimed by a kernel driver), `0x5c`, `0x5d`, `0x76`.

**Bus 1 — three devices only:** `0x73`, `0x75`, `0x77`.

## The SFP/PHY expanders are behind a mux

`0x22` (SFP presence) and `0x23` (SFP `TX_DISABLE`) — the addresses the vendor's
board code uses — **do not answer on either bus**. Bus 1's `0x73/0x75/0x77`, and
bus 0's `0x76`, are all in the classic PCA954x multiplexer range, so those
expanders are almost certainly on downstream segments.

Reaching them means selecting a mux channel, which is a **write** to board
control hardware. That is a normal, reversible operation, but it is a different
category from anything above and is not done here. Confirm the parts at those
addresses really are muxes before writing to them — the address range is
suggestive, not proof.

## Why this matters beyond I2C

The BCM84848 copper PHY behind the four RJ45 ports answers nothing on MDIO —
measured across all 8 buses at all four of its addresses. The most likely reason
is that it is held in reset, and this board gates port hardware through exactly
these expanders. So this bus is the probable route to the RJ45 ports, and it is
now open.

## Probing policy

`ffn_i2cscan.py` probes by **reading**, never by writing. `i2cdetect`'s default
mode uses SMBus "quick write" over part of the range — a zero-length write to an
address nobody has identified — and on a board whose I2C controls PHY resets,
SFP transmitters and power, that is a bad way to find out what is there. The
cost is that write-only devices do not appear; that is the right trade, because
a missed device costs another look and an unintended write costs a hardware
state nobody meant to change.
