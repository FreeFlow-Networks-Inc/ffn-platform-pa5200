# ffn_pcnet -- FFN-owned PCIe virtual Ethernet (MP <-> Octeon CP)

## Why this exists

The vendor PCIC makes the OCTEON DMA descriptors out of HOST memory. That
requires the OCTEON's outbound PCIe path to reach the host -- a path that could
never be made to work from the host side alone, because the host cannot see or
configure the OCTEON end of it (see host-transport/ffn_pcic/PCIC-RING.md, 16
addenda of it). Every register and descriptor was eventually made byte-exact to
the vendor binary and the OCTEON still never fetched.

ffn_pcnet inverts the data flow so the hard direction disappears:

  * the shared rings live in **OCTEON DRAM**;
  * the **host** reaches them across PCIe through the BAR1 index window -- the
    same window ffn_octdram/ffn_cpdp already drive reliably;
  * the OCTEON only ever touches local DRAM.

So the sole cross-PCIe access is host-initiated, which is the direction that
provably works. Nothing depends on the OCTEON mastering the bus. This is the
"own both ends" transport: FFN writes both endpoints, so they agree by
construction and there is no opaque peer to match.

## Memory

Region at OCTEON DRAM **0x29000000**, 4 MB (one BAR1 segment). Verified outside
System RAM (the OCTEON's /proc/iomem shows RAM at 0x00800000-0x01150fff and
0xdff00000-0xffefffff; 0x29000000 is in the unmanaged gap, like cpdp's
0x28000000). This holds on the FFN no-`mem=` boot; do NOT boot the pcnet kernel
with `mem=2G`, which would extend managed RAM over the region.

Host access: BAR1 **index 1** (cpdp owns index 0). Write
`spem0_bar1_index1 = 0xa43` (= `((0x29000000>>22)<<4)|3`) via vendor
oct-remote-csr, once; then BAR2 (resource2) offset **0x400000** maps to DRAM
0x29000000. No contention with cpdp or the boot mailbox.

## Layout (octeon/pcnet/ffn_pcnet.h)

    +0x000000  header: magic "FFNPNET1", version, geometry, host_up, oct_up
    +0x001000  H2O ring: host produces (MP->CP, the bulk NFS direction)
    +0x200000  O2H ring: OCTEON produces (CP->MP)

256 slots x 2048 B per ring. head/tail single-producer/single-consumer, no lock.
All control fields big-endian (OCTEON-native, host swaps). len doubles as the
per-slot ready flag; each frame carries a CRC32 (zlib-compatible) so a coherency
slip is detected, not acted on. Producer writes payload+crc+len then advances
head with a `sync` between; consumer reads head, verifies, clears len, advances
tail.

Direction asymmetry is deliberate: host BAR writes are posted/fast, host BAR
reads serialise/slow. Bulk NFS is files MP->OCTEON = host writes = the fast side.

## Endpoints

  * host: `tools/ffn_pcnetd.py` -- points index 1, resets the region (host owns
    init), TAP `ffnnet0` @ 127.1.1.1/24, bridges TAP <-> rings.
  * OCTEON: `octeon/pcnet/ffn_pcnetd` -- static BE MIPS64, mmaps /dev/mem at the
    region, TAP `ffnnet0` @ 127.1.1.2/24 (self-configured via ioctls; also
    mknod's /dev/net/tun and sets route_localnet), bridges TAP <-> rings.

127.1.1.x is deliberate: 127/8 is non-routable, so the link cannot be reached
from any physical topology -- the PCIe-only isolation the security constraint
requires. NFS exports are already scoped to 127.1.0.0/16.

## Validation

`tools/test_pcnet.py` proves the whole protocol with no OCTEON boot: host via the
index-1 BAR, the OCTEON side stood in by cpdp memrdblk/memwrblk (which run on the
OCTEON and touch its local DRAM). Genuine host-BAR-write vs OCTEON-CPU-read
coherency, both directions, CRC-checked. PASSES: H2O 6/57/1280-byte frames, O2H
frames, ring drains empty.

## Kernel

FFN's OCTEON kernel needed `CONFIG_TUN=y` (it was not set); rebuilt from
/mnt/clones/kbuild with the SDK gcc 4.7 (HOSTCFLAGS="-fcommon -O2" for the old
dtc), stripped to ffn-vmlinux-octeon3. Host kernel already has TUN.

## Status / next

Transport proven at protocol level. Endpoints written and built. Next: boot the
TUN kernel, run both daemons, ping 127.1.1.1<->127.1.1.2, then `mount -t nfs
127.1.1.1:/opt/dpfs` on the OCTEON. Throughput is userspace+MMIO for now;
kernelise if needed.
kernelise if needed.

## TODO: replace manual address/index assignment with an allocator

Region bases (0x28000000 cpdp, 0x29000000 pcnet, kernel/overlay/ffn_mem) and
BAR1 index numbers (0 cpdp, 1 pcnet) are hand-assigned today, and safety is
hand-verified against /proc/iomem. That does not scale as regions multiply. Plan
a small reserved-DRAM allocator once the transport is proven:

  * discover the unmanaged DRAM gap from the boot descriptor / iomem;
  * hand out aligned, non-overlapping named regions;
  * allocate + program a free BAR1 index per region;
  * publish a registry both endpoints read, so no base or index is hardcoded.

Deferred deliberately until pcnet works end to end, so it factors a proven
layout rather than a speculative one.

## NFS-root: a full OS backed by NFS over pcnet

Goal (per the design): the OCTEON runs a FULL Linux userland served from the MP's
SSD over NFS-over-pcnet, not a userland carried in the initramfs. The initramfs is
only enough to bring up pcnet, NFS-mount the MP's rootfs, and enter it. This is
the vendor's "unified storage" model, done post-boot over FFN's own transport
instead of kernel nfsroot.

`/opt/dpfs` on the MP IS that full rootfs (bin, lib, usr, Python, bash, the DP
stack). `octeon/pcnet/ffn-nfsroot.sh` (baked into the overlay as /sbin/ffn-nfsroot):
starts ffn_pcnetd, waits for 127.1.1.1, `ffn_nfsmount 127.1.1.1:/opt/dpfs`, then
chroots into it (switch_root is the production form once init parity is sorted).

### ffn_nfsmount -- the mount helper, and the freestanding-MIPS lessons

The lean initramfs has no mount.nfs and busybox dropped its NFS client, so
`mount -t nfs` fails (no helper). `octeon/pcnet/ffn_nfsmount.c` is a ~2.7 KB
static helper that just hands the kernel's text-based NFS mount API an options
string via mount(2); the in-kernel NFS client does the portmapper/mountd RPC
itself. Building it freestanding on MIPS n64 took three fixes, each a classic:

  * `-O2` gave `_start` a prologue that moved $sp before we could read argc/argv
    -> SIGSEGV. Fix: `_start` is a pure asm stub that passes the entry $sp to a C
    `ffn_main` before any frame is set up.
  * default static-PIE left string-literal addresses unrelocated -> SIGSEGV
    dereferencing "nfs". Fix: `-no-pie -fno-pie`.
  * the n64 PIC ABI needs `gp` established by crt0, which `-nostdlib` skips, so
    every gp-relative GOT load faulted. Fix: `-mno-abicalls -fno-pic -G0` for
    absolute addressing. This was THE one that mattered.

Build: `mips64-linux-gnuabi64-gcc -O2 -EB -mabi=64 -mno-abicalls -fno-pic -G0
-no-pie -nostdlib -static -Wl,-e,_start`.

### Operational lessons (OCTEON lean initramfs)

  * Applets are NOT on PATH: call `busybox chmod`, `busybox mkdir`, `busybox rm`,
    `busybox ls`. Bare `chmod`/`rm` silently do nothing ("command not found").
  * DO NOT hand-transfer binaries over the serial console. push_to_octeon.py
    (printf-hex) is unreliable: without `busybox rm` the file APPENDS across
    transfers and corrupts (2752 B binary became 14 KB of concatenated copies),
    and the console output is too garbled to verify. Bake binaries into the
    overlay cpio instead -- exact bytes, correct mode, no transfer.
  * The overlay rootfs is `rootfs` (rw) but has no separate /tmp mount; exec works
    from it once mode is 755 via busybox chmod.
