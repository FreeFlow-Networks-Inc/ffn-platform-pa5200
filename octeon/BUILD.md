# Building FFN's Octeon kernel for the PA-5220 (CN73XX)

Reproduces the image at `/var/lib/ffn-ngfw/octeon/ffn-vmlinux-octeon3`, which boots on the 5220's
OCTEON III with the 2x10G BGX2 NIF trunk up (`eth0`/`eth1`, `carrier=1`).

## Sources and licensing

* Kernel: `SDK-5.1.0/OCTEON-SDK/linux/kernel/linux` — Linux **4.9.57**, GPLv2.
* Compiler: the SDK's own prebuilt `mips64-octeon-linux-gnu-gcc` **4.7.0** (same version that built
  the vendor kernel). No distro packages a MIPS64 cross toolchain we need, and none is required.
* `initramfs/ffn_init.c` — **FFN's own code**, freestanding, no libc.
* The board's device tree is **read from the box's own u-boot at runtime** (`ffn_fdt=`). It is never
  packaged: a vendor-generated FDT is vendor content, fine to use in place, not to redistribute.

## Config deltas from `cavium_octeon_defconfig`

| Symbol | Value | Why |
|---|---|---|
| `OCTEON3_ETHERNET` | y | the BGX/PKI/PKO dataplane NIC driver |
| `OCTEON_BGX_PORT`, `OCTEON_BGX_NEXUS` | y | so BGX2 lmac0/lmac1 probe |
| `BLK_DEV_INITRD`, `INITRAMFS_SOURCE` | y, `/mnt/clones/initramfs` | the vendor kernel has initrd OFF, so it could only ever root over NFS |
| `DEVTMPFS`, `I2C_CHARDEV` | y | `/dev` without a populated rootfs; i2c access |
| `SERIAL_8250_NR_UARTS`, `SERIAL_8250_RUNTIME_UARTS` | 8 | at the default of 2 the Exar XR17V354 takes both slots and the Octeon's own UARTs fail with -28 (ENOSPC) |
| `SERIAL_8250_PCI` | n | keeps the Exar from claiming the name `ttyS0`, which sends the console out a port nobody reads |
| `NUMA` | n | not needed once the boot descriptor is validated (see the patch) |

## Build

    S=/path/to/SDK-5.1.0/OCTEON-SDK
    patch -p0 -d $S/linux/kernel/linux < octeon/patches/0001-bootdesc-fdt-validation.patch
    patch -p0 -d $S/linux/kernel/linux < octeon/patches/0002-ffn-rootfs-overlay.patch
    patch -p1 -d $S/linux/kernel/linux < octeon/patches/0003-ffn-reserve-memauto-fdtname.patch

Apply them in order: 0003 modifies the `ffn_fdt=` handling that 0001 introduces. Note the **`-p1`** on
0003 -- it is a git-style diff with `a/` and `b/` prefixes, whereas 0001 and 0002 carry absolute paths
and need `-p0`.

`octeon/tests/` holds a host-side unit test for the 0003 arithmetic. It `#include`s the code
**extracted verbatim** from the patched `setup.c`, so it tests what actually ships rather than a
retyped copy:

    python3 octeon/tests/extract.py          # pulls the blocks out of the patched setup.c
    cc -Wall -Wextra -O2 -o t octeon/tests/ffn_reserve_test.c && ./t

40 checks: exclusion arithmetic over the real 4 MB chunk pattern, malformed input, the repeated-
parameter form, and the `ffn_mem=auto` reserve derivation. `extract.py` pulls line ranges, so if you
add a function above the extracted block it fails loudly at compile time rather than silently testing
stale code.

    $S/tools/bin/mips64-octeon-linux-gnu-gcc -O2 -mabi=64 -march=octeon3 \
        -mno-abicalls -fno-pic -fno-stack-protector -ffreestanding -nostdlib \
        -Wall -Wl,-e_start -o $INITRAMFS/init octeon/initramfs/ffn_init.c

    make -C $S/linux/kernel/linux ARCH=mips O=$B \
        CROSS_COMPILE=$S/tools/bin/mips64-octeon-linux-gnu- \
        HOSTCFLAGS="-fcommon -O2" -j$(nproc) vmlinux

`HOSTCFLAGS="-fcommon -O2"` is required: this tree plus a modern host GCC otherwise fails with
`multiple definition of 'yylloc'` in `scripts/dtc`.

**The source tree must be clean before the first `O=` build**, or that build fails part-way through
`archprepare` with

    arch/mips/kernel/asm-offsets.c:11:
    include/linux/preempt.h:59:25: fatal error: asm/preempt.h: No such file or directory

The cause is a previous IN-tree build having left `arch/mips/include/generated` and
`include/generated` in the source tree: kbuild then never writes the generated `asm/` header wrappers
into `$B`, and the first file to need one dies. `make mrproper` in the source tree fixes it. The
failure is easy to misread, because an output directory that has already been built in keeps working
— it has its own generated headers — so only a *fresh* `$B` shows the problem. If other people are
reading that SDK tree, copy it (839 MB) and `mrproper` the copy rather than theirs.

Then strip and stage:

    $S/tools/bin/mips64-octeon-linux-gnu-strip -o ffn-vmlinux-octeon3 $B/vmlinux

## Boot

    python3 tools/ffn_octconsoled.py start          # one owner of /dev/ttyS1
    python3 tools/ffn_octctl.py boot --dev 0 --force # reset + reload u-boot
    python3 tools/ffn_octboot.py --watch 120

Watch alongside it with `tail -f /var/log/ffn-octeon-console.log`. Never open `/dev/ttyS1` directly
while the broker holds it: a UART has one reader, and two readers each take a share of every reply,
which looks exactly like a dead line or a wrong baud rate.

Resulting command line:

    bootoctlinux 0x21000000 numcores=8 console=ttyS0,115200n8 \
      ffn_rootfs=0x22000000,<len> ffn_reserve=0x22000000,<len> rw \
      ffn_mem=auto,256M ffn_reserve=0x28000000,1M ffn_reserve=0x29000000,4M

That is what `tools/ffn-octeon-up.sh` emits, verified on hardware 2026-09-01. Three things in it are
worth understanding before changing any of them.

**No `ffn_fdt=`.** With 0003 the kernel finds the tree by looking up the `cvmx_bootmem` named block
`__fdt` through the descriptor at `/proc/octeon_info:phy_mem_desc_addr`, so the per-board constant is
gone. `ffn_fdt=` still works and still wins as an override. **The kernel and the boot line must be
deployed together**: dropping `ffn_fdt=` against a kernel without 0003 falls through to the
uninitialised `octeon_bootinfo->fdt_addr` and the boot dies in `octeon_irq_init_ciu`.

**`ffn_reserve=` is repeated, never a `;` list.** `;` is the u-boot command separator and this line is
executed by u-boot: an unescaped one truncates the line and silently drops every argument after it --
the later ranges, and `mem=`/`pktbuf=`/`wqe=` too. u-boot runs the first half, boots, and reports
nothing. A backslash-escaped `\;` survives, but repetition needs no escaping at all. (Escaped `;` is
the safer choice where one script must feed *many kernel versions*: repetition against a kernel
predating 0003's outer loop keeps only the first range, silently.)

**The overlay reserve is required, not tidiness.** The overlay is staged before Linux starts but
consumed by Linux in `do_populate_rootfs`, i.e. after the allocator is live, so the kernel will hand
those pages out before the unpacker reads them. Booting without it on 2026-09-01 gave
`FFN: no cpio at 0x22000000 (magic ffffff80000000), skipping` -- kernel data written over the staged
cpio -- and the CP came up with no overlay, hence no `/sbin/ffn-nfsroot` and no NFS root. Its size is
emitted by `ffn_octboot.py` from the same `len(blob)` that sizes `ffn_rootfs=`; do not write it as a
literal here, or the two derivations drift and the reserve under-covers a grown payload while the
boot line still reads correctly.

### SUPERSEDED by `ffn_mem=auto` -- kept for the trap, not the recommendation

**Read this for the `memparse()` trap and the measurements. Do not follow its recommendation:**
patch 0003 adds `ffn_mem=auto[,<reserve>]`, which sizes memory from what the board reports and
removes the per-board `mem=` constant entirely. `mem=` still works and still wins.

Why auto is better than a correct constant: `plat_mem_setup()` already computes the right figure and
throws it away -- `cvmx_bootmem_available_mem(mem_alloc_size)` sums the cvmx free blocks at least one
allocation unit large, which is exactly what the allocation loop can consume -- and then clamps it to
`max_memory`, whose compile-time default is 512 MB. `ffn_mem=auto` uses that figure minus a reserve,
because what Linux claims comes out of the cvmx free list and the FPA pools are allocated from that
list later; starving them gives `cvmx_fpa3_pool_populate: out of memory` and a dead packet engine that
presents as a misprogrammed ring. Reserve is `2 * (pktbuf + wqe) + max(avail/16, 256 MB)`, and the
`avail/16` term is primary -- `pktbuf=`/`wqe=` are a bring-up placeholder on a board whose engine has
never initialised, so a reserve derived from them alone would be arithmetic over a number that does
not mean what it looks like.

One measured warning about *any* `max_memory` change, `mem=` or auto: **it moves which physical
addresses Linux manages, non-obviously.** cvmx hands out large blocks first, so fewer requests leave
the low region untouched. On this CP, `mem=8G` put the second block at `0x20300000`, bare
`ffn_mem=auto` (reserve 510 MB) moved it to `0x30300000`, and `ffn_mem=auto,256M` brought it back to
`0x20700000` -- a 254 MB change in the reserve moved the FFN transport regions in and out of managed
RAM. So never conclude a region is safe from a measurement taken at one `max_memory`, and keep its
`ffn_reserve=` even on a boot where the log says it excluded nothing: the reservation is the only
thing that does not depend on allocator ordering.

The original section follows.

This section previously said "do not add `mem=`, the bootmem descriptor sizes DRAM
correctly by itself". **That is wrong, and it cost us real work.** It conflated a
genuine trap with a false conclusion.

The trap is real: `arch/mips/cavium-octeon/setup.c` parses this with
`memparse()`, so a **bare** `mem=2048` means 2048 *bytes*, not megabytes, and
boots into something unusable.

The conclusion was wrong. Booting with no `mem=` at all leaves the kernel with
whatever the boot descriptor happens to offer, which on this board is
**~432 MB of the 8 GB the CP actually has**. Measured, not theorised:

    /proc/device-tree/memory/reg    0x0        + 0x010000000   =  256 MB
                                    0x20000000 + 0x1F0000000   = 7.75 GB
    /proc/meminfo MemTotal          442400 kB
    /proc/iomem System RAM          0xdff00000-0xffefffff plus 9 MB low

So Linux took only the top ~512 MB below the 4 GB line and ignored the rest.

**Use `mem=2G` or `mem=2048M`** -- with a suffix. That is what
`ffn_octboot.py --extra 'mem=2G'` is for, and its own comment has said so all
along; this file was the one that disagreed.

Why it matters beyond tidiness: the vendor SDK sizes its `sw_state` region from
`stable_size`, which `runningConfig.soc` shows PAN running at **250 MB**. That
cannot fit alongside a 187 MB `bcm.user` in 432 MB, and the failure surfaces as an
unrelated-looking SAL mutex assertion at `sync.c:554` rather than as an
out-of-memory error. On 2 GB it is a non-issue.

---

## Cores

`CIU_FUSE` reads `0xff`, so this CN73XX has **8** cores (not 16). Boot with `numcores=8`
(`ffn_octboot.py` now defaults to it); the kernel then reports `present/online/possible = 0-7`.

## The dev overlay rootfs (DEV ONLY)

`patches/0002-ffn-rootfs-overlay.patch` adds `ffn_rootfs=<addr>,<size>`, which unpacks an additional
cpio the host staged in Octeon DRAM, *after* the built-in initramfs. This is how the vendor MIPS64
userland is used without ever entering an FFN image -- `CONFIG_INITRAMFS_SOURCE` would bake it into
the kernel itself. The overlay contains no `/init`, so it cannot displace FFN's own, and the newc
magic is checked before unpacking so a wrong address is reported rather than fed to the unpacker.

    bash octeon/initramfs/build_overlay.sh      # from this appliance's own /opt/dpfs
    # -> /tmp/ffn-dev-overlay.cpio, stage to /var/lib/ffn-ngfw/octeon/dev/
    python3 tools/ffn_octboot.py                # stages it and adds ffn_rootfs= automatically

Two traps when building it: `/opt/dpfs` is a staging tree with **x86-64 files mixed in** (its own
`sbin/init` is x86-64), so filter on the ELF header rather than the path; and the libraries are
**symlinks**, so `cp -a` leaves dangling links and nothing execs -- use `cp -aL`.

**Never package this cpio into an FFN image.** It is vendor content, used in place on the box that
already owns it. It lives under `/var/lib/ffn-ngfw/octeon/dev/`, not in the repo.

The vendor busybox has no `sh`, no `devmem`, no `--install`, and no applet symlinks. After boot:

    for x in $(busybox --list); do busybox ln -sf /bin/busybox /bin/$x; done

## ffn_mem, and loading tools into a running Octeon

`tools/ffn_mem.c` is FFN's own physical peek/poke (freestanding MIPS64, no libc; commands on stdin so
it needs no argv handling). Build it like `ffn_init.c` but add `-G0`.

To get a binary onto an already-running Octeon without rebuilding the kernel, stage it in DRAM from
the host and pull it out through `/dev/mem`:

    # host: ffn_octdram write to e.g. 0x24000000, padded to a page multiple
    # octeon:
    dd if=/dev/mem bs=4096 skip=$((0x24000000/4096)) count=2 of=/bin/ffn_mem
    chmod 755 /bin/ffn_mem

## Reaching the FE100 (BCM88375)

It comes up with memory decode **off** -- every BAR read returns `0xffffffff` until:

    echo 1 > /sys/bus/pci/devices/0001:01:00.0/enable

BAR0 `0x11c0101800000` (32 KB), BAR2 `0x11c0100800000` (8 MB, holds the CMIC window). Registers are
little-endian against the Octeon's big-endian reads, so **byte-swap every 32-bit access**: BAR0 reg0
reads `0x75830000`, i.e. device ID `0x8375`.

Front-panel LEDs are the CMIC LED microprocessor, at BAR2 + `0x20000` (`CTRL`), `0x20050`
(`CLK_PARAMS`), `0x2005c` (`CLK_DIV`), `0x20400` (`DATA_RAM`), `0x20800` (`PROGRAM_RAM`). `CTRL`
reads 0, so the processor is not running -- that is why the lights are dark.

## Kernel modules

`CONFIG_MODULES=y` and `MODVERSIONS` is off (no symbol-CRC checks), and 25 symbols are `=m` — but
`make vmlinux` alone builds **no** `.ko`. Build and install them into the initramfs:

    make -C $S/linux/kernel/linux ARCH=mips O=$B \
        CROSS_COMPILE=$S/tools/bin/mips64-octeon-linux-gnu- \
        HOSTCFLAGS="-fcommon -O2" -j$(nproc) modules

    make -C $S/linux/kernel/linux ARCH=mips O=$B \
        CROSS_COMPILE=$S/tools/bin/mips64-octeon-linux-gnu- \
        INSTALL_MOD_PATH=$INITRAMFS INSTALL_MOD_STRIP=1 modules_install

    rm -f $INITRAMFS/lib/modules/4.9.57/{build,source}   # dangling in a cpio

Then rebuild `vmlinux` so the initramfs is repacked. 20 modules, ~1.1 MB stripped. `modules_install`
runs depmod for you, so `modules.dep` is generated and `modprobe` resolves dependencies. Unlike the
dev overlay these are **FFN's own build from GPL source, so they belong in the embedded initramfs**
and are shippable.

Verified on the box: `modprobe octeon-sha1` → rc=0, pulling in `sha1_generic`; `lsmod` shows the
refcount; `rmmod` and direct `insmod <path>.ko` both rc=0; kernel reports `Not tainted`.

This also brings up the **Octeon hardware crypto**: `/proc/crypto` lists `octeon-sha1`,
`octeon-sha256`, `octeon-sha512` and built-in `octeon-md5`, all `selftest: passed` at priority 300,
so they take precedence over the software implementations.

**The vendor `fe100.ko` cannot be loaded here** — it is built for 3.10.87-oct2-dp and the vermagic
will not match 4.9.57. The module path above exists so FFN's *own* FE100 driver can be a module; the
vendor one is reference material only, read statically.

## CP <-> DP transport over PCIe

Lets the x86 control plane program the OCTEON dataplane. `octeon/transport/ffn_cpdp.h` is the shared
ABI, `octeon/transport/ffn_cpdpd.c` the DP daemon, `tools/ffn_cpdp.py` the CP client.

Rather than reimplementing PAN's PCIC ring format, it uses the two paths already proven on this
hardware: the SLI/BAR window on the CP (`ffn_octdram`, byte-exact over 20 MB) and mmap of `/dev/mem`
on the DP. A ring pair in OCTEON DRAM at **0x28000000**, polled, no interrupts. FFN's own protocol,
so it ships; `pcic.ko` stays reference material.

Three design points that are not optional:

* **Every shared field is big-endian.** The OCTEON is big-endian and x86 is little-endian, so one
  defined order is required or both sides read garbage. Big-endian keeps the DP free of swapping and
  the CP packs with Python `>` formats.
* **Each message carries a CRC32 of its payload** (reflected, matching `zlib.crc32`). On OCTEON the
  L2 is the coherence point for both cores and the IOB, so a cached core mapping does see host PCIe
  writes — the CRC is there so that if that assumption is ever wrong it is *detected*.
* **Single producer / single consumer per ring**: `head` is written only by the producer, `tail` only
  by the consumer, so no locking. The superblock magic is published **last**, so the CP never acts on
  a half-built region.

The daemon also enables the FE100 PCI memory decode at startup, because it comes out of every OCTEON
boot with decode off and every BAR read returning `0xffffffff`.

Start it on the DP, then drive it from the CP:

    /bin/ffn_cpdpd &                       # on the OCTEON

    python3 tools/ffn_cpdp.py ping         # DP alive. version=1 magic=OK
    python3 tools/ffn_cpdp.py info         # cores : 8   kernel: 4.9.57
    python3 tools/ffn_cpdp.py link         # eth0/eth1 flags=0x1043 up=yes carrier=1
    python3 tools/ffn_cpdp.py memrd 0x1b020000 --width 8 --count 4
    python3 tools/ffn_cpdp.py memwr <addr> <val> [--width 8|16|32|64]
    python3 tools/ffn_cpdp.py fe100rd 0x20050 --count 4      # byte-swap applied
    python3 tools/ffn_cpdp.py fe100wr <bar2-off> <val>
    python3 tools/ffn_cpdp.py led 0        # LEDUP0 CTRL / CLK_DIV

`MEM_RD`/`MEM_WR` are the universal primitive — with them the CP can reach BGX, the FE100 BARs, both
CPLDs, and the CMIC LED unit. The `FE100_*` ops are conveniences that bake in the BAR2 base and the
byte-swap so callers cannot forget either.

Verified on hardware: write path confirmed at 32- and 64-bit by writing through the transport and
reading the same DRAM back through the CP's own SLI window (`a5a5a5a5...1122334455667788`), which
validates the transport and the coherency assumption together. Writes into `CMIC_LEDUP0_DATA_RAM` do
**not** stick while the LED processor is stopped — that is the hardware, not the transport.

## The transport daemon starts at boot

`ffn_cpdpd` ships inside the embedded initramfs at `/sbin/ffn_cpdpd`; FFN's init spawns and supervises
it. Init reaps with `wait4(-1, WNOHANG)` rather than a blocking wait, so a dead daemon is noticed even
while an interactive shell sits idle, and it respawns whichever child exited. Verified on a clean boot
with nothing staged by hand:

    FFN> started /sbin/ffn_cpdpd pid 995
    ffn_cpdpd: up, region 0x28000000 magic published
    ffn_cpdpd: FE100 PCI memory decode enabled

## The embedded initramfs is now FFN-only

It previously carried the vendor userland — `bin/busybox` (468 KB), `lib64/ld-2.16.so` (964 KB) and
`lib64/libc-2.16.so` (10.8 MB) — so every kernel built before this point had ~11.8 MB of vendor glibc
and busybox baked in. That is a real violation of the ship-only-our-own-code rule, not a theoretical
one, and it is worth re-checking after any initramfs change:

    find $INITRAMFS -type f | xargs file | grep -v 'lib/modules'

The image now holds only `/init`, `/sbin/ffn_cpdpd`, the device nodes, and the 20 FFN-built `.ko`
modules — **1.2 MB**, cpio 299 KB, stripped kernel 8.3 MB (was ~12 MB). Both binaries are freestanding
so nothing needs a libc. The vendor bash/libc userland still exists for dev work, but only through the
DRAM-staged `ffn_rootfs=` overlay: used in place, never packaged.
