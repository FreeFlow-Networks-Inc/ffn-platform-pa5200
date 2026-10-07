# ffn_nac — NAC (Nir-Aho-Corasick) accelerator driver for the PA-5200

Own-code driver for Palo Alto's family of hardware Aho-Corasick pattern
matchers. Primary target is the **CE40**, PCI `feed:a00d`.

This replaces the earlier CA1-only driver in `octeon/ca1/`, which is superseded
and should be deleted once you are happy with this one.

## The family is one design

Every vendor module — `cheetah`, `ocelot`, `ce40`, `ce10`, `ca1`, `ca2` —
carries the identical modinfo description, **"PAN Nir-Aho-Corsik (nac)
Driver"**. The vendor's own PDT settles that they are one design rather than
six chips:

```
$ sed 's/ce40/ca1/g; s/CE40/CA1/g' usr/share/pdt/ce40.py | diff - usr/share/pdt/ca1.py
$ echo $?
0
```

1791 lines each, **zero** differing lines. Same register map, same
`cip_partition_offset = 0x8800 + 0x1000*i`, same
`dfa_partition_offset = 0xc800 + 0x1000*i` (so one part does both the
Aho-Corasick CIP tables and the DFA tables), same per-link registers at
`0x4004 + 0x200*link`.

So the register map in [BAR0-MAP.md](BAR0-MAP.md) — measured on CA1 — applies
to the CE40 as well, and one driver covers all six.

| id | variant | capabilities (measured, see below) |
|---|---|---|
| `feed:a00d` | **ce40** | ddr-layouts, mem-check, **rld-cal** |
| `feed:a00e` | ce10 | mem-check |
| `feed:a101` | ca1 | ddr-layouts |
| `feed:a102` | ca2 | ddr-layouts |
| `feed:a009` | ocelot | rld-retry |
| `feed:a003` | cheetah | — |

`feed:f101` and `feed:fe1c` belong to `fe100.ko`. The FE100 is a flow engine,
not a NAC, and is not handled here.

### Where the capability bits come from

Not guessed from part numbers — read out of each vendor module's own strings
and symbols:

| bit | evidence |
|---|---|
| `ddr-layouts` | exports `nac_ddr_layout_small` / `_medium` / `_large` |
| `mem-check` | `"<NAME> memory check failed"`, `"FPGA memory link is down - aborting!"` |
| `rld-cal` | `check_rld_status()`, `"FPGA RLD CAL"`, `"Get RLD CAL ERROR"`, `"Get RLD Link issue, try to recover"` |
| `rld-retry` | `"RLDRAM Memory init retry %d"` / `"succeeded"` / `"incomplete"`, but no `check_rld_status` |

## Why CE40 and not CA1

We wrote this for CA1 first. CA1 *is* on our board — `0003:06:00.0`, on the
control plane's own PCIe domain, invisible from the x86 MP and visible once the
OCTEON CP boots. It binds, it answers, it was mapped. It is still a dead end on
this platform, for two measured reasons:

**1. PAN-OS 11.2.4-h5 ships no driver for it.** The installed DP module tree
holds `nac-ce10`, `nac-ce40`, `nac-cheetah`, `nac-ocelot` and nothing else; a
`sudo find` over the whole sysroot turns up no `ca1.ko` or `ca2.ko`. The vendor
OS that ran on this appliance for its entire service life could not talk to
`feed:a101` at all. That is a much stronger statement than "the logs never
mention CA1" — there was never a driver to mention it.

**2. CA1 has no memory bring-up.** Its memory-channel ready bit never rises
(BAR0 `+0x0b8` reads `0x00000002`, bit 0 clear) and `ca1.ko` contains no
recovery path. The giveaway is in the printk levels: the string
`"Memory channels init incomplete"` is level **3** (KERN_ERR) in `ca1.ko` and
level **4** (KERN_WARNING) in `ce40.ko` — because CE40 has
`check_rld_status()`, RLD calibration and link recovery, and CA1 has none.

CE40 is also the member the vendor's CP board agent actually configures, first
thing, on every boot:

```
Loading: libfpga.so...
Setting CE40_ILKPHY_CFG to 0x72.
libfpga.so done
...
Resetting CE40 (link 0)
```

and we already load its bitstream ourselves from FFN's own boot path.

## Not yet verified: is CE40 on the PCI bus?

**Do not record "CE40 works" until a probe line from real silicon says it
bound.** The CE40 is an FPGA and may only enumerate as a PCI endpoint once
programmed; the PCI inventories we have were taken on boots where it was not.
The driver binds it if it is there and stays quiet if it is not.

To check, with the CP booted on FFN:

```bash
ls /sys/bus/pci/devices/*/device | while read f; do printf '%s %s %s\n' "${f%/device}" "$(cat "${f%/device}"/vendor)" "$(cat "$f")"; done | grep -i 0xfeed
```

Expect `0xfeed 0xa00d` for the CE40. `0xfeed 0xa101` is the CA1 we already know
about, and `0xfeed 0xfe1c` is the FE100.

## What the driver does

Two honest things: it turns the device on, and it gives you a safe instrument
for mapping the register space.

With no driver bound, PCI `COMMAND` reads `0x0000` — memory decode **off** — and
every BAR reads back `0xffffffff`, which looks exactly like dead silicon. It
isn't: the BARs are assigned and the link is up, the device just isn't decoding
memory cycles. `pci_enable_device()` fixes that:

```
ffn_nac 0003:06:00.0: enabling device (0000 -> 0002)
ffn_nac 0003:06:00.0: PCI COMMAND 0x0000 -> 0x0142 (MEM decode ON)
ffn_nac 0003:06:00.0: NAC variant ca1 (feed:a101) ready as /dev/ffn_nac0 (READ-ONLY) ddr-layouts
```

### Deliberately absent

No register semantics, because we do not have them. The vendor surface
(`nac_aho_read/write`, `nac_dfa_read/write`, `nac_mem_take/release`,
`nac_ddr_layout_*`) gives the *shape* — a chardev over a large window with
separate Aho and DFA table regions and three DDR layouts — not the offsets.

**In particular there is no RLD calibration here**, even though `ce40.ko` has
one. We have its existence, not its registers. A calibration sequence written
from a guessed offset would be worse than none: it would look like bring-up and
be noise.

`ca1.ko` carries no DWARF (only `.mdebug.abi64`), so there are no struct
definitions to recover the way the `pci_dma` protocol was.

## Safety

- **Writes are refused** unless loaded with `allow_write=1`.
- **Bus mastering is never enabled**, so the device cannot DMA into host memory.
- All MMIO is 32-bit aligned — unaligned access on MIPS64 BE traps.
- Probe **refuses** an id that is in the PCI table but not the variant table,
  rather than binding with `caps = 0` and silently claiming a part has no RLD
  calibration when it has one.

## The 32-bit access rule — read this before touching the window

**These devices only honour 32-bit accesses.** It is the most expensive thing
we learned about them.

`memcpy_fromio()` is free to copy byte-wise, and when it does, every sub-word
access returns `0xff` — so the whole window reads back all-ones and looks like
dead silicon, *while a plain `ioread32()` of the very same address returns real
data*. It is also 4× the transactions, each paying a PCIe completion timeout,
which turned a single 4 KiB read into a ~22 second uninterruptible kernel stall
that held the device mutex and could not be killed.

So: **explicit `ioread32`/`iowrite32` loops, never `memcpy_*io`.** The driver
does this; don't "optimise" it back.

`mmap` is deliberately not implemented for the same family of reasons — it
returned `ffffffff` where `read()` returned `00000000` at the same address.

## Byte order

A property of the *window*, not the device. The vendor driver settles it: in
`nac_probe` the layout selector is read with a bare `lw` off BAR0 — no swap —
while every table accessor wraps its access in `wsbh`+`ror 0x10`, the MIPS
32-bit byteswap idiom.

| BAR | order | accessor |
|---|---|---|
| 0 | big-endian | `ioread32be` |
| 1 (AHO table) | little-endian | `ioread32` |
| 2 (DFA table) | little-endian | `ioread32` |
| 3 | big-endian (assumed; untouched by those accessors) | `ioread32be` |

Default `be_mask=0x9`. It is a module parameter, so a sibling NAC part that
disagrees can be driven without a rebuild. **Measured on CA1** — re-check it on
the CE40 rather than assuming.

## Building

Build on the **RE VM**, not the MP and not the CP:

- the CP has no toolchain at all (no `gcc`, `cc`, `make` or `ld`, no headers);
- the MP has no mips64 cross compiler;
- the CP's `/lib/modules/$(uname -r)/build` already points at
  `/mnt/clones/fwdport/debian-candidates/linux-cp`, a path on the RE VM — the
  tree the running kernel was built from, and the only one with a matching
  `.config` and `Module.symvers`.

```sh
make KDIR=/mnt/clones/fwdport/debian-candidates/linux-cp \
     ARCH=mips \
     CROSS_COMPILE=/mnt/clones/fwdport/gcc-14.4.0-nolibc/mips64-linux/bin/mips64-linux-
```

The kernel.org **nolibc** crosstool is the right one — kernel modules don't link
libc. No chroot needed.

Verify before shipping:

```
file ffn_nac.ko      -> ELF 64-bit MSB relocatable, MIPS, MIPS64 rel2
modinfo ffn_nac.ko   -> vermagic must match the running CP kernel EXACTLY
                        (currently 6.18.49-ffn-debian-cp-dirty; the `-dirty`
                        is real and expected, the tree carries FFN's patches)
                        aliases must include pci:v0000FEEDd0000A00D
```

## Deploying

The CP NFS-roots on `/opt/ffn-cproot`, an ordinary directory on the MP's SSD, so
deployment is a plain copy. The 91-package CP root has no `kmod`:

```sh
make install-cp
ffn-cp "busybox insmod /lib/modules/extra/ffn_nac.ko"
```

## Mapping the register space

`ffn_nacscan.py` runs on the CP (CPython, no compiler there):

```sh
ffn_nacscan.py info                  # variant, caps, COMMAND, BAR geometry
ffn_nacscan.py dump --bar 0 --len 256
ffn_nacscan.py live                  # words that are neither 0x0 nor 0xffffffff
ffn_nacscan.py volatile              # words that change by themselves
```

`live` finds the addresses the device actually decodes; `volatile` finds
counters and status registers, the strongest evidence a block is running.

**Gotcha:** MIPS does not use the asm-generic ioctl encoding. It follows the
sparc/alpha family — `_IOC_SIZEBITS` is 13 not 14, `_IOC_DIRBITS` is 3 not 2, so
the direction field starts at bit **29** not 30. Encoding `_IOR` the generic way
produces a request number the driver rejects with `ENOTTY`. The struct size is
part of that request number, so the scanner's `_INFO_SIZE` and the kernel's
`sizeof(struct ffn_nac_info)` must agree exactly: both are **168** bytes,
verified by `offsetof` on x86-64 and mips64 BE alike.

## Status

- **CA1 (`feed:a101`)** — verified on silicon: builds, loads, binds, enables
  memory decode (`COMMAND 0x0000 → 0x0142`), reads real register data. BAR0 is
  mapped in [BAR0-MAP.md](BAR0-MAP.md).
- **CE40 (`feed:a00d`)** — driver retargeted and cross-built clean for the CP
  kernel; **presence on the bus not yet confirmed**, see above.
- No register semantics are implemented for either.
