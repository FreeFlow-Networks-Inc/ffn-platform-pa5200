# Booting the 40-core DP Octeon from the 8-core CP

Status: **not booted.** The DP is present, powered and enumerated; the vendor
`oct-remote-*` tools refuse to recognise it. Everything below is measured on the
live PA-5220, not inferred.

## The two Octeons

| | CP ("MP Octeon") | DP Octeon |
|---|---|---|
| part | CN73XX / CN76XX | CN78XX |
| cores | `numcores=8` | `numcores=40` (PA-5220), 48 on 5250/5260/5280 |
| seen from | x86, `0000:01:00.0` `177d:9700` | **CP**, `0003:03:00.0` `177d:0095` |
| boot script | `/sbin/octeon` (runs on x86) | `/opt/dpfs/sbin/octeon` (runs on the CP) |
| DRAM | 8192 MB (`/proc/octeon_info`) | — |

The DP is **not on the x86 bus**. It hangs off the CP's own PCIe behind a PLX
PEX8606, which is why its boot script runs on the CP.

`FFN boots the CP.` Any DP property — the 40 cores, the per-core FIPS table,
Qumran DDR tuning — must not be attributed to the CN73XX.

## What is confirmed working

* The DP endpoint is real: class `0x0b30` (MIPS processor), BAR sizes
  **8 MiB / 64 MiB / 1 MiB / 64 KiB** — byte-identical to the endpoint layout the
  x86 sees on the CP.
* `/proc/bus/pci/0003:03/00.0` exists, so the domain-qualified path the vendor
  library opens is present.
* FFN's CP kernel provides everything those tools need: `/dev/mem`,
  `/proc/bus/pci`, `/proc/octeon_info`.
* The vendor MIPS tools now run on FFN's CP (staged in the dev overlay):
  `oct-remote-{reset,boot,load,bootcmd}` + `liboct-remote_cp.so.1`, SDK 3.1.2-p5
  build 580.
* **The DP is powered.** CE CPLD reg 1 = `0xff`, and bit 6 of that register is
  "CAV power" per the vendor's own decoder. All rails good.

## Where it stops

```
oct-remote-boot --devnum=0 --scantwsi   ->  "Octeon device not found"  (rc 255)
```

Enabling PCI memory decode did not change it (it did walk the PLX chain).
Reading the DP's BAR0 through the CP<->DP transport:

```
DP  BAR0 0x11e010400000 = 00000000 00000000 00000000 00000000
BCM BAR0 0x11c0101800000 = 75830000 11000000      <- correct device id
```

All-**zeros**, not all-ones. All-ones is an undecoded BAR (what the BCM and FE100
did before their enable bit was written); all-zeros means the endpoint answers but
the silicon behind it is not running. Combined with the device id reading `0x0095`
rather than a normal Octeon id, the DP is sitting pre-initialised.

## The CPLD, and why it is a dead end for this

The CPLDs live on the **CP Octeon's MIO boot bus**, not the x86 LPC bus. From
`cpldlib_init` / `ce_cpldlib_init` in `libpancommon_cp.so.1.0`:

```
v1 = 0xfff0 <<16 | 0x23 ; dsll32 11 ; ori 0x10   ->  0x8001180000000010
                                       ori 0x18  ->  0x8001180000000018
```

`0x8001_0000_0000_0000 | addr` is `CVMX_ADD_IO_SEG`, so those are physical
`0x1180000000010` and `0x1180000000018` = **MIO_BOOT_REG_CFG2** and
**MIO_BOOT_REG_CFG3**:

* **main CPLD = boot-bus chip select 2**
* **CE CPLD  = boot-bus chip select 3**

Base address is computed at runtime, not hardcoded:

```
base = ((MIO_BOOT_REG_CFGn & 0xFFFF) << 16) | (1 << 63)
```

and access is plain 8-bit MMIO — `cpldlib_reg_write` is literally
`sb a1, 0(base + offset)`. No chardev, no locking. (The x86 has a *separate*
system CPLD reached over LPC via `lpccpldlib`; that is not this one.)

**But dpreset is not implemented on the 5200:**

* `ce_cpldlib_dpreset` is `jr ra; nop` — a literal no-op.
* `cpldlib_dpreset` calls a GOT slot that resolves to
  `extServiceFuncNotImplementedCalled` — a stub that reports "not implemented".
* No binary anywhere in the sysroot contains a real dpreset implementation; only
  `libpancommon_{cp,dp}.so` even mention the name.

So the PDT `dpreset` command is a diagnostic convenience that was never wired up
on this platform. The vendor's real DP reset is `oct-remote-reset --devnum=N`,
i.e. done **over PCIe**, not through the CPLD.

## Live CPLD register values

Read from CP u-boot (`cpld_reg display` / `ce_cpld_reg display`) with the console
broker stopped — two readers on one UART garbles every reply.

```
main CPLD (CS2), regs 0x00-0x14:
  00:13  01:ff  02:49  03:00  04:00  05:00  06:00  07:00  08:40  09:60
  0a:3c  0b:02  0c:0a  0d:00  0e:00  0f:00  10:1f  11:60  12:1d  13:00  14:06

CE CPLD (CS3), regs 0x00-0x0a:
  00:0c  01:ff  02:20  03:00  04:00  05:00  06:00  07:00  08:c7  09:3c  0a:90
```

Decoded so far, from the vendor's own `ce_cpld.py`:

* CE reg 0 = CPLD version (12)
* CE reg 1 = power rails: bit6 CAV, bit5 2V5, bit4 1V8, bit3 1V5, bit2 1V2,
  bit1 1V1, bit0 VTT. `0xff` = all good.

Registers 2, 8, 9, a are non-zero and undecoded — the likely home of reset and
strap controls. `cpld_reg write [0-a] [hex]` exists in CP u-boot if they need
probing, but nothing should be written there without knowing what it drives.

## Recommended path

Do **not** chase the vendor library's device-id match. FFN already implements this
exact protocol: `ffn_oct.py` has `BootBar`, `oct_remote_boot` (loadcache a u-boot
into L2 and start it) and `oct_send_bootcmd` (mailbox), which is how the x86 boots
the CP today. The DP boot is the same protocol one level down.

Port it to run against the DP's BARs. The clean split, given the CP has no Python:

* x86 orchestrates (it already has `ffn_oct.py`),
* the CP executes the register writes — add DP-boot ops to `ffn_cpdpd`, alongside
  the existing MEM_RD/MEM_WR/BCM ops.

That keeps it FFN's own code, so it ships.

Then boot **FFN's own kernel** on the DP, not the vendor
`vmlinux-3.10.87-oct2-dp`. The vendor DP kernel NFS-roots from the x86 through the
CP (`nfsroot=/opt/dpfs,v3`, `ip=127.1.2.2:127.1.1.1:127.1.2.1:...`), which needs
the vendor `if_pci`/`fabric_vif` modules built for their 3.10 kernel — those cannot
load on FFN's 4.9.57 CP. FFN's kernel with an embedded initramfs has no such
dependency, and CN78XX is OCTEON III so the same `-march=octeon3` build applies;
only `numcores` changes.

## Notes for image patching

* `build_overlay.sh` now stages `oct-remote-{reset,boot,load,bootcmd}`, their
  libraries, and both DP u-boot images, strips the tools (~1.55 MiB -> ~792 KiB
  each), and **fails the build if the overlay exceeds 32 MiB** — staged at
  `0x22000000` it would otherwise run past `ffn_mem` at `0x24000000`. Adding the
  tools unstripped had already taken it to 40.1 MiB. It is now 25.5 MiB.
* `oct-remote-csr` is deliberately excluded: 7.9 MiB even stripped, apparently
  carrying a built-in CSR database. Worth re-adding when that is wanted.
* The dev overlay is vendor content, staged into DRAM, never packaged.
* `ffn_oct.py` on the appliance was stale (995 lines, missing
  `oct_send_bootcmd` and `_configured_octeon_gen`) and broke
  `ffn_octctl.py cmd`. The build server's copy (2498 lines) is canonical and was
  synced over it. `_configured_octeon_gen()` reads
  `/etc/ffn-ngfw/octeon-gen`, which provision.sh writes from
  `FFN_OCTEON_GEN` in the model profile.

## Vendor userland identity

Both roots are **CentOS 7.2.1511**. The x86 management plane runs glibc 2.17; the
dpfs Octeon root carries the same CentOS 7 release files cross-built for MIPS64
big-endian with glibc 2.16. So the Octeon userland is a cross-compiled CentOS 7,
not a separate distro.

This matters for FFN: the CP shell works today only by borrowing that MIPS64 BE
glibc, which is usable in place but not redistributable. A shippable Octeon
userland means building one — no current distro ships MIPS64 **big-endian**
(Debian's mips64el is little-endian) — so Buildroot or musl-cross for
`mips64-octeon-linux-gnu`. Memory is not the constraint: the CP has 8 GB.

---

# Addendum: DP boot ops delivered, and the one remaining gate

## Ops added to ffn_cpdpd (built, deployed, verified)

| op | code | signature |
|---|---|---|
| `FFN_OP_MEM_WRBLK` | 15 | `a0=addr a1=len`, payload = bytes |
| `FFN_OP_MEM_RDBLK` | 16 | `a0=addr a1=len` -> payload |
| `FFN_OP_PCI_CFG_RD` | 17 | `a0=off a1=width`, payload = sysfs path -> `a1`=value |
| `FFN_OP_PCI_CFG_WR` | 18 | `a0=off a1=width a2=value`, payload = sysfs path |

CLI: `ffn_cpdp.py memwrblk <addr> <file>`, `memrdblk <addr> <len> [--out f]`,
`cfgrd <path> <off> [--width]`, `cfgwr <path> <off> <val>`.

Verified on hardware:

* 4048-byte payload round-trip, sha256 identical
* 4096-byte **file** round-trip through the CLI, `cmp` identical, 2 messages
* a transfer straddling a 64 KB mapping boundary
* argument validation rejects `a1 != payload len` and oversize reads
* `cfgrd +0x00/32` = `0x95177d`, `cfgrd +0x04/16` = `0x6` after a `cfgwr`

Why they matter: `MEM_WR` moves one value per message, so a 1.2 MB bootloader is
~150k round trips each paying an mmap/munmap pair. The block ops move a full
payload and map once per 64 KB window — the same transfer is ~312 messages. The
config ops exist because part of the boot sequence is not in MMIO at all.

`bcopy_v` uses 64-bit moves where alignment allows, falling back to bytes at the
edges, because the PCIe BAR windows on this board do not reliably accept
byte-granular writes while DRAM does not care.

## Current DP state, measured through the new ops

```
cfgrd +0x00/32  = 0x95177d          vendor 177d, device 0095
cfgrd +0x04/16  = 0x0000  -> 0x0006 memory space + bus master were BOTH OFF
capabilities      PM@0x40  MSI@0x50  PCIe@0x70  MSI-X@0xb0
PMCSR@0x44      = 0x0008            D0 already; bit3 = NO_SOFT_RESET
BAR0 0x11E0104000000              all-ones
BAR2 0x11E0100000000              all-ones
```

So the DP is in **D0**, memory decode and bus mastering are now **enabled**, and
its BAR windows still do not answer. The endpoint claims config-space
transactions and not memory ones, which means the Octeon's internal fabric is
not out of reset.

Two earlier readings were wrong and are corrected here:

* An apparent live word at `BAR0+0x0c = 0x01000000` was read while the command
  register was `0x0000` — memory decode off. It was an artifact.
* A first pass tested `0x11e010400000`, one hex digit short of the real
  `0x11e0104000000`. The resource file is authoritative:
  BAR0 `0x11e0104000000` (8 MiB), BAR2 `0x11e0100000000` (64 MiB),
  BAR4 `0x11e0104800000` (1 MiB).

Also note `enable`, the command register and the PLX chain all reset when the CP
reboots, so any DP state has to be re-established after each CP boot.

## The remaining gate

The vendor's x86 tool prints, when booting the CP:

```
[1] power state now D0
[2] reset: Setting OCTEON for flash boot.
[3] boot: Setting OCTEON for remote boot. | Loading u-boot into L2 cache ... | Powering up additional cores.
```

Step 2/3 is the part that makes a dead BAR window live, and it happens before
anything is loaded. The MIPS build of that tool, staged on the CP, refuses with
`Octeon device not found` — its enumeration does not accept device id `0x0095`,
and there is no environment variable or config file to override it (checked).
The CPLD `dpreset` path is a stub on this platform, so that documented route
does not exist either.

**Next step: disassemble the x86 `oct-remote-boot`, not the MIPS one.** It is a
normal x86-64 binary whose code path demonstrably works — it boots the CP today.
What it writes to make the CP's endpoint window live is the same thing the DP
needs, and reading a working path is far cheaper than fighting the MIPS build's
enumeration. The CP can now perform both halves of whatever that turns out to
be: config space via ops 17/18, memory via ops 15/16.

## On using NFS to have the DP pull instead

Reasonable instinct — it is what the vendor does, but only for the *rootfs*, and
it is strictly more work here, not less. `nfsroot=/opt/dpfs,v3` with
`ip=127.1.2.2:127.1.1.1:127.1.2.1:...` means the DP reaches an NFS server on the
x86 *through the CP*, over IP-over-PCIe provided by the vendor `if_pci` +
`fabric_vif` modules. Those are built for their 3.10 kernel and cannot load on
FFN's 4.9.57, so NFS presupposes FFN writing its own virtual-Ethernet-over-PCIe
driver on both sides first. The bootloader and kernel are pushed over PCIe in
either case; only the rootfs would come over NFS. An embedded initramfs needs
none of that, so it stays the right first step — NFS becomes attractive later,
once a vif exists and the rootfs outgrows an initramfs.

## On the newly pulled Marvell tools

* `/mnt/clones/marvell-tools-13004.0.tar.bz2` (337 MB) targets
  **`aarch64-marvell-linux-gnu`** — the ARM-based OCTEON TX2 / CN9xxx line. It
  cannot build anything for CN73XX or CN78XX, which are MIPS64. Not usable here.
* `/mnt/clones/toolchain-build-53.tar.bz2` (273 MB) **is** the Octeon toolchain,
  as a build tree rather than a prebuilt compiler: `scripts/build-octeon-linux`,
  `build-octeon-native`, `build-octeon-simple-exec`, and
  `linux-headers/include/asm-mips/`. This is the GCC-7.3 lead worth pursuing —
  SDK 5.1 ships GCC 4.7.0, so a 7.3 MIPS64 cross-compiler would open up building
  a modern kernel for the DP instead of the 4.9.57 the CP runs.

---

# Addendum 2: the reset write, why the vendor tool cannot reach the DP, and a full rootfs

## The write, recovered from the x86 tool

`liboct-remote_mp.so.1.0`, function **`pci_reset`**, writes one CSR and then
settles:

| field | value |
|---|---|
| CSR address | `0x0001010000000100` (`CIU_PP_RST`) |
| value written | `0xffe` |
| settle delay after the write | 5 ms |

The path is gated on `octeon_pci_model`, compared against a list of supported
OCTEON part numbers.
`0x0001010000000100` is FFN's `CIU_PP_RST_LEGACY`. **`0xffe` releases core 0 and
holds cores 1..11 in reset** — FFN's `oct_remote_boot` writes `0` there, which
releases every core. Worth reconciling. The path is gated on `octeon_pci_model`,
compared against `0xd9300`/`0xd9000`/`0xd9100`/`0xd0300`/`0xd0400`/`0xd0600`/`0xd0700`.

## How a CSR address reaches the chip

```
write_csr(addr,val)
 └─ write_mem64(addr,val)                 byte-swaps the value for a BE target
     └─ remote_funcs+0x38(addr,&val,8)    block write, installed by the PCI backend
         ├─ pci_bar1_setup()              programs octeon_pci_bar0_bar1_index
         └─ data moves through octeon_pci_bar1_ptr
```

`pci_bar1_setup(addr)` encoding, read straight off the disassembly:

```
idx = ((addr >> 22) & 0x3fff) << 4 | 0x3     ; 0x3 = END_SWP | ADDR_V
*(u32 *)(bar0_ptr + bar1_index_off) = idx    ; then read back to flush
```

which matches the recorded `SPEM0_BAR1_INDEX0 = 0x3` exactly. Model-specific BAR0
offsets the vendor carries:

| | older (`0xd0300`/`0xd0600`) | newer (`0xd0700`) |
|---|---|---|
| `bar1_index` | `0x100` | `0x0` |
| `win_rd_addr` | `0x8` | `0x210` |
| `win_rd_data` | `0x20` | `0x240` |

**Cavium BAR numbering is not Linux resource numbering.** Cavium BAR0 =
`resource0` (8 MB), **Cavium BAR1 = `resource2` (64 MB)**, Cavium BAR2 =
`resource4` (1 MB). The DP's sizes match that exactly.

Note BAR1 **cannot** reach CSR space: the index is 14 bits at 4 MB granularity,
so 64 GB of reach, while `CIU_PP_RST` sits at `0x0001_0100_0000_0100`. CSR access
therefore goes through the SLI indirect window (`win_*_addr`/`win_*_data`) in
BAR0, not through BAR1. BAR1 is for DRAM.

## Why the vendor MIPS tool cannot address the DP

With `OCTEON_PCI_DEBUG=1` the library says what it is doing:

```
0:0.0 PCI ID 0x177d9700 is an Octeon. BAR0=0x11e010600000c[0x800001] BAR1=0x0[0x0]
Octeon device not found
```

It matched the CP's **own root port**, saw it has no BAR1, and stopped — it never
considered the DP. The cause is structural: the library parses
`/proc/bus/pci/devices`, whose first column is `bus<<8 | devfn` **with no PCI
domain**. This CP has four domains and three Octeon root ports that all render as
`0000`. The DP renders as `0300`.

Env vars found (I was wrong last turn to say none existed):
`OCTEON_PCI_IDS`, `OCTEON_PCI_DEVICE`, `OCTEON_PCIE_QLM`, `OCTEON_PCI_DEBUG`,
`OCTEON_REMOTE_PROTOCOL`, `OCTEON_REMOTE_SCRATCH_ADDRESS`. `OCTEON_PCI_DEVICE`
does shift which device is examined — `0300` landed on the BCM and reported
`PCI ID 0x14e48375 not recognized` — but no combination reached `177d:0095`, and
that same message proves `OCTEON_PCI_IDS` did not widen the accepted set. The
tool is the wrong vehicle on a multi-domain host.

## Attempting the write directly: blocked at the hardware

With the DP's command register confirmed at `0x0006` (memory space + bus master,
re-set in the same run because **every CP reboot clears it**):

```
BAR0 +0x0000  before=0xffffffff  after=0xffffffff  no change
BAR0 +0x0008  before=0xffffffff  after=0xffffffff  no change
BAR0 +0x0020  before=0xffffffff  after=0xffffffff  no change
BAR0 +0x0100  before=0xffffffff  after=0xffffffff  no change
BAR0 +0x0210  before=0xffffffff  after=0xffffffff  no change
BAR0 +0x0240  before=0xffffffff  after=0xffffffff  no change
```

BAR0 accepts no writes at any candidate offset. So the BAR1 index register cannot
be programmed and `CIU_PP_RST` cannot be reached over PCIe. The code stops there
rather than writing blind into a window that is not answering.

Conclusion unchanged and now much better evidenced: the DP's endpoint answers
config space but claims no memory transactions, i.e. its internal fabric is in
reset, and the release is board-level. Both vendor `dpreset` entry points are
stubs, so it is not in the vendor userland.

## A full Linux rootfs for the Octeon

Built by `octeon/rootfs/build_dprootfs.sh`, in three formats:

| image | size | use |
|---|---|---|
| `ffn-dprootfs.squashfs` | 55 MB | read-only root, pair with overlayfs |
| `ffn-dprootfs.ext4` | 420 MB | writable root via `root=/dev/ram0` |
| `ffn-dprootfs.cpio` | 318 MB | raw newc, the proven `ffn_rootfs=` path |

Contents: a real userland — `bash`, coreutils, `python2.7`, `openssl`, `vi`,
`find`, `gdb` — 9768 entries, plus FFN's own `init`, `ffn_cpdpd`, `ffn_bcmctl`
and `ffn_bcm.ko`, and the four `oct-remote-*` tools. **Six `libcrypto` FIPS
integrity HMAC sidecars survive**, which is the RHEL FIPS mechanism.

Derived from the vendor's MIPS64 big-endian CentOS 7.2.1511 tree with the 1.55 GB
PAN application stack (`usr/local`) and docs/man/locale/include removed: 1.9 GB
down to 314 MB, and 55 MB compressed.

**Licensing: dev use only.** That tree is vendor content — usable in place on a
box that owns it, never packaged into a distributed FFN image.

### On Rocky Linux

Not possible as a port. Rocky builds x86_64, aarch64, ppc64le and s390x — there
is no MIPS target at all, let alone MIPS64 **big-endian**. Debian's `mips64el` is
little-endian. The vendor did not install a distro here either; they cross-built
a CentOS-7-flavoured userland, which is what the `.hmac` files and glibc 2.16
show. A shippable FFN rootfs has to be cross-built the same way — Buildroot for a
controlled minimal userland, or Yocto/OpenEmbedded if RPM packaging and systemd
are wanted.

### Kernel support added for this

Previously absent, now enabled and verified on the booted CP
(`/proc/filesystems` shows `squashfs`, `overlay`, `ext4`, `nfs`, `nfs4`; and
`/dev/ram0` exists as `brw------- 1,0`):

```
CONFIG_SQUASHFS=y  CONFIG_SQUASHFS_XZ=y  CONFIG_OVERLAY_FS=y
CONFIG_BLK_DEV_RAM=y  CONFIG_BLK_DEV_RAM_SIZE=2097152 (2 GB)
CONFIG_CRYPTO_MANAGER_DISABLE_TESTS=n     (FIPS needs the self-tests)
```

`CONFIG_CRYPTO_FIPS` is still off, blocked on `CONFIG_MODULE_SIG` while
`CONFIG_MODULES=y`. Three ways forward, none of which should be chosen silently:
enable `MODULE_SIG` with a build-time key; build the Octeon kernel monolithic
(`MODULES=n`, fold `ffn_bcm` in) so signing is moot; or do userland-only FIPS via
the OpenSSL FIPS provider. The middle option suits the Octeon, which loads almost
nothing dynamically.

Caveat worth stating plainly: OpenSSL 3's FIPS provider cross-builds and runs its
self-tests, but CMVP validation is per-module-per-build through a lab. FFN can be
FIPS-capable and run in FIPS mode; "validated" is a separate, paid process.

### DRAM staging map

```
0x21000000  kernel image
0x22000000  dev overlay cpio        (32 MB budget, guarded in build_overlay.sh)
0x24000000  ffn_mem
0x28000000  CP<->DP transport ring  (1 MB)
0x40000000  full rootfs cpio        <- new, clear of everything above
```

---

# Addendum 3: the CP had 432 MB, and what the vendor kernel tells us

## FFN's CP kernel was running with 432 MB of an 8 GB chip

The full-rootfs boot failed like this:

```
ffn_rootfs=0x40000000,0x12fc8800
FFN: unpacking overlay rootfs, 0x12fc8800 bytes at 0x40000000
FFN: overlay rootfs failed: write error
```

Size and magic were both fine; it ran out of room. `/proc/meminfo` explained why:

```
MemTotal:  442416 kB      <- 432 MB, on a chip with 8 GB (dram_size: 8192)
```

Cause: FFN boots with **no `mem=` at all**, so the kernel takes whatever the boot
descriptor offers. The earlier decision to drop `mem=` was right about the
hazard and wrong about the remedy — `arch/mips/cavium-octeon/setup.c` parses it
with `memparse()`, so the vendor's suffix-less `mem=2048` really would mean 2048
**bytes**; but `mem=2G` or `mem=2048M` is parsed correctly and is what should
have been passed.

Fixed by adding `--extra` to `ffn_octboot.py` and booting with `mem=2G`:

```
MemTotal: 1988656 kB      <- 1.9 GB
```

**Consequence for the DRAM map:** with `mem=2G` the kernel manages 0..2 GB, so
`0x40000000` (1 GB) is no longer outside its map and anything staged there can be
reallocated. Staging for a large rootfs has to move above the `mem=` ceiling —
`0x80000000` for `mem=2G`.

Revised map:

```
0x21000000  kernel image
0x22000000  dev overlay cpio        (32 MB budget, guarded)
0x24000000  ffn_mem
0x28000000  CP<->DP transport ring  (1 MB)
   ...      kernel-managed RAM up to the mem= ceiling
0x80000000  full rootfs cpio        (just above a mem=2G ceiling)
```

Anyone raising `mem=` must move the rootfs staging address to match. That
coupling is easy to miss and produces exactly the silent "write error" above.

## The vendor's Octeon kernel, for reference

```
Linux version 3.10.87-oct2-dp (build@712ceccc4c6f)
  (gcc version 4.7.0 (Cavium Inc. Version: SDK_BUILD build 49)) #25 SMP
  Sun Sep  8 10:10:13 PDT 2019
```

* 9,020,038 bytes, the only Octeon kernel in the sysroot
  (`/opt/dpfs/boot/vmlinux-3.10.87-oct2-dp`, symlinked `vmlinux.oct2-dp`).
* Built with the Cavium SDK's own gcc 4.7.0 — the same compiler version FFN uses
  from SDK 5.1.
* Carries `CONFIG_CAVIUM_OCTEON2` and refuses to run otherwise:
  `"ERROR: CONFIG_CAVIUM_OCTEON2 not compatible with this processor"`.
* Cavium platform drivers present: `octeon_bgx_nexus.pki_port`,
  `octeon_power_throttle`, `octeon_hw_status`.
* **No embedded config** — `IKCONFIG` is off, so `extract-ikconfig` returns
  nothing. Their Octeon `.config` is not recoverable from the image.

`vmlinux_copy()` in `grp_control_plane.py` only stages `vmlinux.mp*` /
`vmlinux.oct2-mp*` into `/var/lib/tftpboot/vmlinux.cp` for PXE-booting 7000-series
line cards; on a pizza box it does nothing useful. Neither `mp` kernel exists in
this sysroot.

Worth weighing before "basing ours on theirs": FFN already boots **4.9.57** on
this hardware with 8 cores and both BGX ports at `carrier=1`. Theirs is
**3.10.87**. Basing on theirs means moving backwards; the parts worth harvesting
are the platform options (BGX nexus / PKI, power throttle, hw status) rather than
the tree.

## The FIPS fork, answered by PAN's own kernel

Their x86 config **is** shipped (`/boot/config-3.10.88-9.0.4.0.54`):

```
CONFIG_CRYPTO_FIPS                  = y
CONFIG_MODULE_SIG                   = (not set)
CONFIG_MODULES                      = y
CONFIG_CRYPTO_MANAGER_DISABLE_TESTS = (not set)   <- self-tests ON
CONFIG_CRYPTO_ANSI_CPRNG            = m
CONFIG_SQUASHFS / CONFIG_OVERLAY_FS = (not set)
CONFIG_BLK_DEV_RAM                  = m
```

So PAN ran `CRYPTO_FIPS=y` **with modules and without module signing** — possible
because on 3.10 `CRYPTO_FIPS` carried no `MODULE_SIG` dependency. That
`depends on (MODULE_SIG || !MODULES)` clause arrived in 4.x, which is why FFN's
4.9.57 refuses the same combination.

The vendor precedent therefore does not transfer directly. Of the three options,
**building the Octeon kernel monolithic (`MODULES=n`, folding `ffn_bcm` in)** is
the closest behavioural match to what they shipped, and it sidesteps key
management entirely. It also suits a plane that loads almost nothing dynamically.
Their choice to leave the crypto self-tests enabled matches the change already
made to FFN's config.
