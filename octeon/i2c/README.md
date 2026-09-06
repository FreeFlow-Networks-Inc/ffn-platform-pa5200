# Control-plane I2C

**Working as of 2026-09-06.** Both OCTEON TWSI buses are reachable from
userspace, without rebuilding or rebooting the control plane.

## What was actually missing

Less than it looked. The device tree already describes both controllers —
`soc@0/i2c@1180000001000` and `soc@0/i2c@1180000001200` — the in-kernel driver
already binds them (`CONFIG_I2C_OCTEON=y`, matching
`cavium,octeon-7890-twsi`), and both adapters were already registered as `i2c-0`
and `i2c-1`.

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
ambiguous. `ffn_i2cread.py` is the tool; it issues the offset write and the read
as **one** `I2C_RDWR`, because split into two calls another master — or the
kernel's own EEPROM driver on the same bus — can interleave and you get a
different register's contents.

## Adapter numbering, and a device tree that does not match the board

`i2c-0` is `i2c@1180000001000` and `i2c-1` is `i2c@1180000001200` (from each
adapter's `of_node`). Note this against the vendor's own numbering below before
using any of their bus numbers.

**Bus 0 — board management.** `0x1a-0x1f`, `0x2a`, `0x30`, `0x31`,
`0x34-0x36`, `0x48-0x4b` (temperature, verified), `0x52-0x55` (DIMM SPD,
verified), `0x57`, `0x5c`, `0x5d`, `0x76`.

**Bus 1 — three devices, all in the multiplexer address range:** `0x73`, `0x75`,
`0x77`.

**`tlv-eeprom@57` is on the wrong controller.** The device tree places it under
`i2c@1180000001200`, so the kernel dutifully instantiates `1-0057` — and
**nothing answers at 0x57 on bus 1** (`ENXIO`). Something does answer at 0x57 on
bus 0, but reads back all `0xff` at both 1-byte and 2-byte offsets, so it is not
serving TLV content either. Do not treat `1-0057` as the board EEPROM; it is a
device node for hardware that is not at that address. The vendor's own CP device
tree, extracted from `u-boot-gryphon_cp_pciboot.bin` at offset `0x8f010`, has the
same placement, so this is inherited, not an FFN error.

## How the vendor reaches everything else, from their own code

This is the part worth knowing before writing anything. `libports.so`'s
`gryphon_read_sfp_state` reads SFP presence like this — call names recovered with
`tools/ffn_mipscall.py`, argument constants included:

```
gryphon_sfp_i2c_map(port)                  -> bit index
libi2c_open(bus=9, &h)
libi2c_read_cmd_byte(&h, addr=0x22, 0)     -> low byte
libi2c_read_cmd_byte(&h, addr=0x22, 1)     -> high byte
libi2c_close(&h)
state = ((hi << 8) | lo) >> bit & 1
```

Two byte registers at offsets 0 and 1 is a PCA9555-shaped expander, and it lives
on **bus 9**. There is no bus 9 in hardware. `libi2c_open` in
`libpancommon_cp.so` is 0xcc bytes and does exactly this:

```c
snprintf(path, 32, "/dev/i2c-%d", bus);    /* rodata at 0x5f3960 */
fd = open(path, O_RDWR);
```

**So the vendor's "virtual bus" numbers are literally Linux i2c adapter
numbers.** Their kernel instantiates the multiplexers, Linux creates a child
adapter per channel, and their userland just opens it. Their PDT source
(`/usr/share/pdt/i2c.py`) names several: Gryphon CE on bus 26, power supplies on
27, fan trays on 30 and 31, SFP module EEPROMs from bus 10 up.

### Which settles how FFN should do this

Not by writing channel-select bytes from userspace. Declare the multiplexers so
the kernel's `i2c-mux-pca954x` driver owns them — then channel selection is
arbitrated with the bus lock held, each channel appears as its own `/dev/i2c-N`,
and the vendor's bus numbers become directly meaningful. Hand-poking a mux from
userspace races with any other master on the bus and can leave a transaction
landing on the wrong segment.

The parts are not yet *proven* to be muxes. A bare read — no offset byte, nothing
on the wire but the address, `ffn_i2cread.py --bare` — returns `0x00` from all of
`1-0x73`, `1-0x75`, `1-0x77` and `0-0x76`. That is what a PCA954x reports with no
channel selected, which also explains why nothing downstream answers today. It is
consistent, and it is the strongest evidence obtainable without a write, but
`0x00` is not a signature. Confirm the part numbers before committing a DT node.

### Some faceplate state is not on I2C at all

The same function's other branch does not touch I2C. It reads
`CVMX_GPIO_RX_DAT` via `__cvmx_gpio_read` and shifts out a per-port bit — lines
2, 5, 7, 14 and 15. So part of the faceplate presence state is wired straight to
OCTEON GPIO, needing no muxes and no writes.

**That path is now open — see `../gpio/README.md`.** Two corrections to what this
file first said about it:

- The address is `0x8001070000000880`, not `0xFF80000107000880`. The constant is
  assembled across four instructions (`lui`/`ori`/`dsll32`/`ori`) and the
  intermediate value is not the address; the result is XKPHYS-uncached plus
  physical `0x1070000000880`, which is the GPIO block base `0x1070000000800`
  plus the driver's `RX_DAT` offset of `0x80`. Same register either way, but the
  wrong number is not worth keeping.
- "This kernel exposes no gpiochip" was right about the symptom and wrong about
  the cause. `/sys/class/gpio` is absent because `GPIO_SYSFS` is off; the modern
  interface is `/dev/gpiochip*`. `CONFIG_GPIO_OCTEON=y` was already set and
  nothing needed enabling — the driver simply never bound, because the DT says
  `cavium,octeon-7890-gpio` and upstream matches only `-3860-`.

## Probing policy

`ffn_i2cscan.py` probes by **reading**, never by writing. `i2cdetect`'s default
mode uses SMBus "quick write" over part of the range — a zero-length write to an
address nobody has identified — and on a board whose I2C controls PHY resets,
SFP transmitters and power, that is a bad way to find out what is there. The
cost is that write-only devices do not appear; that is the right trade, because
a missed device costs another look and an unintended write costs a hardware
state nobody meant to change.

`ffn_i2cread.py` is the deliberate step up: a register read must transmit the
register index, so it puts a byte on the wire. It therefore takes an **explicit**
address and never sweeps. `--bare` is the exception with no write at all, and is
the right probe for a part you have not identified.

## Why this matters beyond I2C

The BCM84848 copper PHY behind the four RJ45 ports answers nothing on MDIO —
measured across all 8 buses at all four of its addresses. This board gates port
hardware through these expanders, so this bus is a probable route to the RJ45
ports.

The vendor's own path there is now readable too: `pan_bcm_84848.c` drives it
through the switch SDK, not through I2C, with an init order of `sysconf_init` →
`soc_phy_common_init` → `soc_phyctrl_software_init` → **`sal_config_set`** →
`pan_read_84848_register`. That `sal_config_set` is the interesting part: the
vendor sets SOC properties *programmatically at runtime*, which is a different
mechanism from the `config.bcm` properties FFN has been using, and is a better
explanation for the PHY never attaching than anything in the config file.
See `bcm/` for that thread.
