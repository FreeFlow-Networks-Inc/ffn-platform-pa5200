# Which distribution can run on the CP and DP

Short answer: **OpenWrt `mips64_octeonplus`**, because it is the only
distribution that targets this ABI. Debian does not exist for it. That is a
fact about Debian, not a judgement about it, and it is worth writing down
because "just use Debian" is the obvious first instinct.

## The hardware constraint

Both OCTEON planes are **big-endian MIPS64, n64 ABI**. Everything on the box
reports it:

```
$ file /opt/dproot/bin/busybox
ELF 64-bit MSB executable, MIPS, MIPS64 rel2
```

and so do the FFN dataplane binary, `nfsd.ko`, and every vendor library
(`libpandp_cp.so`, `libports.so`, `bcm.user`).

## Debian: measured, not assumed

| archive | architecture | result |
|---|---|---|
| `deb.debian.org` bookworm main | `binary-mips64` (BE 64-bit) | **404** |
| `deb.debian.org` bookworm main | `binary-mips64el` (LE 64-bit) | 200 |
| `ftp.ports.debian.org` unstable | `binary-mips64` | **404** |
| `archive.debian.org` bullseye | `binary-mips` (BE 32-bit) | 404 |
| `archive.debian.org` buster, stretch | `binary-mips` (BE 32-bit) | 200 |

Debian Ports carries exactly: `alpha hppa hurd m68k powerpc ppc64 sh4 sparc64
x32`. No mips64 of either endianness.

So Debian's only 64-bit MIPS architecture is **little-endian**, and its
big-endian MIPS was 32-bit `mips`, dropped after buster and now archived and
EOL.

## Why little-endian is not a way out

MIPS is bi-endian and OCTEON III can run little-endian, so `mips64el` looks
tempting. It fails on something we cannot fix:

**The vendor firmware is big-endian and is not rebuildable.** `bcm.user` (the
BCM88375 switch control binary), `brdagent`, the FE100 bundle and the
`libpan*`/`libports` family are all `ELF 64-bit MSB`, we have no sources, and
FFN's policy is to use them in place and never repackage them. A little-endian
platform cannot load them, and without `bcm.user` there is no switch — no
faceplate ports, no path to the dataplane.

Everything else built here would go too: both kernels, the CVMX executive
builds, the FFN dataplane, `ffn_bcm`/`ffn_bde`, and every register-access
assumption (see `bcm/`'s PAXB byte-order and the PCIC register notes — byte
order is load-bearing in several places, not incidental).

## Why 32-bit `mips` is not a way out either

Right endianness, wrong word size: our binaries and kernels are n64. A 32-bit
o32 userland would need the whole toolchain, the dataplane and the SDK rebuilt
for o32, on a 40-core/30 GB machine — and against a Debian release that is
archived and receives no security updates.

## What Debian would actually cost

There is no archive to `debootstrap` from, so this is not an install, it is a
**port**: cross-build binutils/gcc/glibc for `mips64` BE, then bootstrap the
~1000 packages of a base system (the `rebootstrap` process), then maintain a
private Debian architecture indefinitely. Months of work before the first
`apt install`, and a permanent maintenance burden, to reach a userland we
already have.

## What we run instead, and why it is not a compromise

**OpenWrt 24.10.4, `mips64_octeonplus`** — an exact match for this ABI,
confirmed against the CP's own `/etc/openwrt_release` and the OpenWrt toolchain
that builds working binaries for it.

- `opkg` against the MP's caching mirror: **9165 packages**
- Installed and working on the CP today: git, vim, binutils (objdump/readelf),
  gdb, strace, tcpdump, i2c-tools, pciutils, iperf3, socat, rsync, python3,
  bash, htop, less, gawk, tar
- The same distribution, release and ABI on both planes — one userland to
  maintain, not two

## If the real requirement is apt/dpkg specifically

Then the honest options are, in order of cost:

1. **Nothing off the shelf.** No distribution ships dpkg for BE mips64.
2. **Cross-build dpkg + apt for `mips64_octeonplus`** and populate a local
   archive from source. Gets the tooling without the port; every package still
   has to be built.
3. **A full Debian port** (above). Complete, and months.

Worth deciding on the basis of what dpkg is wanted *for* — package breadth,
familiar tooling, reproducible builds, or a specific package — because those
point at different answers, and OpenWrt already covers breadth.
