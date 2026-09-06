# Control-plane GPIO

**Working as of 2026-09-06**, for reads. `/dev/gpiochip0` exists on the live
control plane, 20 lines, no reboot.

Part of the faceplate state this platform needs is not on I2C at all. The
vendor's `gryphon_read_sfp_state` has a branch that never touches a bus: it
reads `GPIO_RX_DAT` and shifts out a per-port bit. This is that path, in our own
code.

## Nothing was disabled — nothing was bound

`CONFIG_GPIO_OCTEON=y`, `CONFIG_GPIOLIB=y` and `CONFIG_GPIO_CDEV=y` were all
already set, and the device tree already describes the block. The driver and the
device both existed and had simply never been introduced:

```
/sys/bus/platform/drivers/octeon_gpio                     <- driver, 0 bound
/sys/bus/platform/devices/1070000000800.gpio-controller   <- device, no driver
```

The device tree says `cavium,octeon-7890-gpio`; upstream's match table lists
only `cavium,octeon-3860-gpio`. No error is reported anywhere, because a
platform device nobody claims is not an error — it presents purely as an
absence. (An earlier note in `../i2c/README.md` read this as "no gpiochip". The
absence was real; the cause was not the driver missing.)

`i2c-octeon` lists both compatibles, which is exactly why the I2C buses bound on
this board without any help and GPIO did not.

## Binding it live, without a reboot

`GPIO_OCTEON` is built in, so fixing the match table properly means a kernel
rebuild and a CP reboot — which takes the switch daemon down with it, and the
dataplane and its PCIe transport after that. `driver_override` avoids all of
that:

```sh
echo octeon_gpio > /sys/bus/platform/devices/1070000000800.gpio-controller/driver_override
echo 1070000000800.gpio-controller > /sys/bus/platform/drivers/octeon_gpio/bind
```

```
gpio gpiochip0: Static allocation of GPIO base is deprecated, use dynamic allocation.
octeon_gpio 1070000000800.gpio-controller: OCTEON GPIO driver probed.
crw-------  254,0  /dev/gpiochip0
```

This is the kernel's own supported mechanism, not a trick: `platform_match()`
consults `driver_override` **first** and short-circuits, bypassing the
`of_match_table`. Manual `bind` on its own will not work — `bind_store()` still
calls `driver_match_device()`, and carries a comment saying it is not possible
to override the driver's id table.

Safe, and specifically checked before running it: `octeon_gpio_probe()`
allocates, ioremaps, fills in the `gpio_chip` and calls
`devm_gpiochip_add_data`. It writes **no** registers. Binding disturbs no pin.

`ffn-gpio-bind.sh` does this idempotently. Reverse with a write to `unbind`.

## Reading

```
$ ffn_gpio.py --read
reading vendor-read lines: 2,5,7,14,15
  line  2 = 1
  line  5 = 1
  line  7 = 0
  line 14 = 0
  line 15 = 0
```

Those five bits are the ones `gryphon_read_sfp_state` shifts out
(`gryphon_ports.c` lines 770/773/777/779/784, recovered with
`tools/ffn_mipscall.py`). Mixed and stable, which a floating or dead read
usually is not. The **polarity is not established** — the vendor's function just
returns the bit — so do not yet read "present" or "absent" off these.

`--info` lists all 20 lines and writes nothing, but its direction column is
gpiolib's own bookkeeping, not measured: `gpio-octeon` implements no
`.get_direction`, so every unrequested line reports the default. All 20 showing
"input" means nothing.

## Reads are correct on this kernel; direction changes are not

This is the part to know before using the tool for anything else.

Reading a value through the GPIO chardev requires requesting the line, and the
v2 uAPI request path has no "leave it as it is" case:

```c
if (flags & GPIO_V2_LINE_FLAG_OUTPUT)
        gpiod_direction_output_nonotify(desc, val);
else
        gpiod_direction_input_nonotify(desc);      /* unconditional */
```

— `gpiolib-cdev.c`, `linereq_set_config`. So any request without the OUTPUT flag
drives the line to input, and `octeon_gpio_dir_in()` writes
`bit_cfg_reg(offset)`. On a line currently driving something real — a PHY reset,
an SFP `TX_DISABLE`, a power enable — that clears `tx_oe` and tri-states it.

**On this chip those writes miss.** Upstream's `bit_cfg_reg()` returns
`8 * offset` for lines under 16, which is the CN3xxx/CN6xxx layout. On CN73XX
the configuration array is flat at `base + 0x100 + offset * 8` for every line —
`CVMX_GPIO_BIT_CFGX` returns `0x0001070000000900 + (offset & 31) * 8` against a
block base of `0x0001070000000800`. So a direction write lands on an undefined
offset instead of `GPIO_BIT_CFG`.

That is why the reads above did no harm: no pin's direction actually changed. It
also means gpiolib's model of every line is decoupled from the hardware. Both
halves of that matter, in opposite directions, and neither is a good place to
stay.

The read path is unaffected — `RX_DAT` at `base + 0x80` is correct on OCTEON III
(`CVMX_GPIO_RX_DAT` = `0x0001070000000880`), and `TX_SET`/`TX_CLR` at `+0x88` and
`+0x90` are correct too. Only the per-line config moved.

`../patches/upstream-6.18/tranche3-modules/0006-gpio-octeon-octeon3-support.patch`
adds the compatible and selects the layout from the match data. It applies and
compiles clean against the CP tree, and is **not** in the running kernel — it
needs the next CP kernel build. Until then, treat this interface as read-only:
`ffn_gpio.py` will not request a line unless told to, and defaults to the lines
the vendor reads, which are already inputs.

## Why this was worth doing

It is the cheaper of the two routes to the faceplate. The other one needs the
I2C multiplexers instantiated to reach the `0x22`/`0x23` expanders — see
`../i2c/README.md`. This one needed no muxes, no writes, and no reboot.
