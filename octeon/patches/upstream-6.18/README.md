# Forward port to upstream Linux 6.18 LTS — tranche 1

**These patches target UPSTREAM `linux-6.18.49`. The series in the parent directory
(`0001`–`0003`) targets the SDK's `4.9.57` tree. They are NOT interchangeable and must never be
applied to the same base** — the 4.9 patches use `add_memory_region`/`boot_mem_map`, which no longer
exist in `arch/mips`, and the 6.18 patch assumes `memblock_add`.

## Status: BOOTS ON HARDWARE

Built warning-clean with the kernel.org crosstool GCC 14.4.0 mips64 (big-endian) against
`linux-6.18.49` + `cavium_octeon_defconfig`. `vmlinux` is 11.3 MB stripped.

**Booted on hardware (PA-5220 CN73XX control plane).** Verified: 8 CPUs, 8.14 GB, all three
PCIe roots enumerating (including the BCM88375 at `0001:01:00.0` and the CN78XX dataplane at
`0003:03:00.0`), NFS root over the PCIe pcnet transport, an OpenWrt userland with `opkg`, and
`ffn_bcm.ko` + `ffn_bde.ko` loading and reaching the chip. Two runtime defects found after this
was first written are fixed in tranche 3: `pcie-octeon.c` marked its `map_irq` handlers
`__init`, so the long-lived `octeon_pcibios_map_irq` pointer dangled and oopsed on the first
post-boot driver bind; and upstream shares one `next_busno` across PCI domains, which hid the
FE100 and the dataplane.

**Config is not a free choice.** See `cp-config-fragment` in this directory: `THP=MADVISE` (not
`_ALWAYS`, which machine-checks at `__update_tlb`) and `PCI_MSI` off (`msi-octeon.c` is CIU-era
and panics on CIU3). `CONFIG_IKCONFIG` is on so the finished image and the running kernel can be
checked with `scripts/extract-ikconfig vmlinux` and `zcat /proc/config.gz` rather than trusting
the tree.

**Still known-bad:** the vendor's statically linked `bcm.user` cannot run on 6.18. With
`THP=MADVISE` it no longer takes the kernel down, but it dies of `SIGBUS` in `do_ade` during
static-glibc TLS startup, before issuing a single BDE ioctl. Two separate defects; only the
first is fixed. BCM/L2 work therefore still runs on the 4.9 CP via the one-shot
`FFN_CP_KERNEL` override.

## Why a forward port is cheap here

Stock upstream 6.18.49 builds for this SoC with **zero patches**, and the symbols the CP needs are in
the resulting image:

    octeon_irq_init_ciu3           PRESENT
    cvmx_bootmem_find_named_block  PRESENT
    plat_mem_setup                 PRESENT

CIU3 — the OCTEON III interrupt controller, and the single biggest risk in the port — is fully
upstream: `octeon_irq_init_ciu3()`, the `cavium,octeon-7890-ciu3` compatible, IP2/IP3 handlers,
mailbox, SMP affinity. (The Cavium upstreaming tracker's note that "all the ciu3 code was deleted"
describes an intermediate state during upstreaming, not the outcome.)

## What this patch contains, and what it drops

| 4.9 patch | fate on 6.18 |
|---|---|
| `ffn_mem=auto` | **DROPPED — obsolete.** Upstream `setup.c` has `max_memory = ULLONG_MAX`; it already takes all memory. The 512 MB cap that patch existed to lift is a Cavium SDK-ism. |
| `ffn_reserve=` | **~180 lines → ~10.** `boot_mem_map`, `add_memory_region` and `BOOT_MEM_RAM` are gone from `arch/mips`; `plat_mem_setup()` calls `memblock_add()` directly, so a reservation is just `memblock_reserve()` with no chunk-splitting. Applied at the END of `plat_mem_setup()`, after every add, so it cannot be undone by a later add — and unlike the 4.9 form it does not depend on allocator ordering. |
| `__fdt` by name | ports as-is (`cvmx_bootmem_find_named_block` is upstream) |
| bootdesc FDT validation | **still required — this is an UPSTREAM bug.** See below. |

247 added lines, against 457 for the 4.9 equivalent.

## The upstream bug this fixes

Upstream `device_tree_init()` does:

    if (octeon_bootinfo->minor_version >= 3 && octeon_bootinfo->fdt_addr) {
            fdt = phys_to_virt(octeon_bootinfo->fdt_addr);
            if (fdt_check_header(fdt))
                    panic("Corrupt Device Tree passed to kernel.");

`fdt_addr` is **not bounds-checked before being dereferenced**. On the PCIe / L2-cache boot path the
bootloader leaves that field uninitialised — observed on a PA-5220 CP as `0x830001756e6b6e6f`, which
is ASCII for the tail of the string `"unknown"` — and `phys_to_virt()` of it is unmapped, so
`fdt_check_header()` takes a fault *before* it can reach the `panic()` that was meant to report the
problem. The boot hangs with no message at all.

This is worth sending upstream once it has been validated on hardware.

## Config changes also required (not in the patch)

The stock `cavium_octeon_defconfig` reproduces a trap already known on this board:

    CONFIG_SERIAL_8250_NR_UARTS=2      ->  8
    CONFIG_SERIAL_8250_RUNTIME_UARTS=2 ->  8
    CONFIG_SERIAL_8250_PCI=y           ->  n

The Exar XR17V354 quad UART on the OCTEON's own PCIe bus claims both 8250 slots, so the internal
UARTs fail with `-28` (ENOSPC) and never become ttys. `console=ttyS0` then binds to the *Exar*, and
all output — including any panic — goes out a port nobody is reading.

Also note `CONFIG_DEVMEM=y` with `CONFIG_STRICT_DEVMEM` unset, exactly as on 4.9. The upgrade does
**not** hand you that hardening: enabling it would break `ffn_cpdpd`/`ffn_pcnetd`, which mmap
`/dev/mem`. That requires moving the transports to a real driver interface first.

## Remaining tranches

2. cvmx executive + register headers — bulk but mechanical, BSD-3 licensed.
3. pcnet/cpdp transports as a chardev/uio driver rather than `/dev/mem` (the `STRICT_DEVMEM`
   prerequisite).
4. BGX + `octeon3-ethernet` — **~4,433 lines** is the real OCTEON III gap
   (`octeon3-ethernet.c` 2679, `octeon-bgx-nexus.c` 728, `octeon-bgx-port.c` 607, plus headers).
   `drivers/staging/octeon/` is back upstream in 6.18 and covers the OCTEON I/II path, so 11 of the
   24 SDK ethernet files already have upstream homes. The MAC half can be aligned against upstream
   `drivers/net/ethernet/cavium/thunder/thunder_bgx.c` (1418 lines) — ThunderX uses the same BGX IP
   and is maintained. The packet path (PKI/SSO/PKO3/FPA3) has no upstream analogue.
5. FPA3/PKI/SSO/PKO3 — only when the packet engine is genuinely being brought up.

Deliberately excluded as not-this-hardware: `ethernet-srio.c`, `octeon-srio-nexus.c` (no SRIO),
`octeon-pow-ethernet.c` (OCTEON II POW era), `octeon-75xx-errors.c` (not this chassis; 73xx for the
CP and 78xx for the DP *are* needed).

## Reproducing the build

    # kernel.org crosstool, mips64-linux is BIG-endian (no "el")
    wget .../pub/tools/crosstool/files/bin/x86_64/14.4.0/x86_64-gcc-14.4.0-nolibc-mips64-linux.tar.xz
    # host deps: flex bison libssl-dev libelf-dev bc zstd
    export PATH=$PWD/gcc-14.4.0-nolibc/mips64-linux/bin:$PATH
    export ARCH=mips CROSS_COMPILE=mips64-linux-
    cd linux-6.18.49
    patch -p1 < .../upstream-6.18/0001-ffn-octeon-6.18.patch
    make cavium_octeon_defconfig
    scripts/config --set-val SERIAL_8250_NR_UARTS 8 \
                   --set-val SERIAL_8250_RUNTIME_UARTS 8 --disable SERIAL_8250_PCI
    make olddefconfig && make -j8 vmlinux

## Userland: embed the Buildroot rootfs, and `ffn_rootfs=` disappears

Tranche 1 also removes the fourth 4.9 patch. Setting `CONFIG_INITRAMFS_SOURCE` to a cpio of the
Buildroot tree makes the kernel self-contained, which deletes the whole `ffn_rootfs=` mechanism
along with the staging region at `0x22000000`, its derived overlay reserve, the two-derivations
hazard, the NFS root, and the vendor `/opt/dpfs` dependency. **So 6.18 needs ONE patch where 4.9
needed four.**

The DP's existing Buildroot output is reusable on the CP as-is -- verified, not assumed. Its binaries
are `ELF 64-bit MSB ... Flags: octeon3, mips64r2`, the same ISA the CN73XX needs, and it carries
glibc, bash, busybox and python3.12.

    unsquashfs -d tree ffn-dp-buildroot.squashfs        # 19.6 MB -> 80 MB, 1837 entries
    cp ffn_init.sh tree/init && chmod 0755 tree/init    # it has sbin/init but NO /init
    chown -R 0:0 tree                                   # a non-root cpio gives uid-1000 files
    (cd tree && find . | cpio -o -H newc > rootfs.cpio)
    scripts/config --set-str INITRAMFS_SOURCE /path/to/rootfs.cpio
    scripts/config --enable RD_XZ --enable INITRAMFS_COMPRESSION_XZ
    scripts/config --enable DEVTMPFS --enable DEVTMPFS_MOUNT

**The self-contained kernel is smaller to stage than what runs today**: 27.9 MB in one piece against
10.1 MB kernel + 27.5 MB overlay = 37.6 MB in two. XZ takes the 80 MB rootfs to 16.6 MB. Confirm the
payload landed via `__initramfs_start`/`__initramfs_size` in `System.map` and the size delta -- the
compressed blob's plaintext will NOT appear in `strings`, so its absence proves nothing.

`ffn_init.sh` is deliberately self-reporting. This is a first boot of a kernel nobody has booted, on
a serial console with no network and no NFS, so it prints the answer to every verification question
at once: MemTotal and core count (does upstream's `ULLONG_MAX` really give the full DRAM with no
`mem=`?), the `System RAM` ranges (did `ffn_reserve=` punch holes?), kpageflags at the four transport
bases, the `FFN:`/`Device Tree` dmesg lines (did `__fdt`-by-name work?), the CIU3 interrupt lines,
and `/sys/class/net`.

**Expect `/sys/class/net` to be EMPTY.** `octeon3-ethernet` is tranche 4, so a boot with no network
is the correct result here, not a failure.
