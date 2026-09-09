# Debian mips64 (big-endian) port — status

Started 2026-09-06 on the RE VM (8 cores, 30 GB RAM, 149 GB free on
`/mnt/clones`), inside a Debian sid amd64 chroot, driven by Debian's own
`rebootstrap`. See `ffn-debian-mips64-bootstrap.sh` for the recipe and
`../USERLAND-DISTRO.md` for why this is a port and not an install.

## 2026-09-08: the port now builds its own packages

The bootstrap is finished and the result is self-hosting: a mips64 BE package
can be built from Debian source, against the port's own libraries, and
published back into the port's own repo. Counts as measured on the VM, not
estimated:

| tree | what it is | packages |
|---|---|---|
| `sid-host/tmp/repo` | the local mips64 archive | 605 `.deb` files, 596 distinct binaries in the `binary-mips64` index |
| `buildroot` | the chroot packages are built *in* | 159 (129 `mips64` + 30 `all`) |
| `cproot` | the root intended for the CP | 68 |
| `images/rootfs-base` | the verified base image | 81 |

`images/debian-sid-mips64-be-base.tar` is 146 MiB with a recorded SHA-256.

### init needed no building

`/sbin/init` was already satisfied: `systemd-sysv 262~rc1-2` is installed in
both `cproot` and `images/rootfs-base`, and `/sbin/init` is a symlink to
`../lib/systemd/systemd`, which is an `ELF 64-bit MSB pie executable, MIPS,
MIPS64 rel2`. Nothing to compile — it came out of the bootstrap. Worth stating
plainly because "build init" sounds like work and was not.

### `build-deb-mips64.sh`

The reusable builder. It serves the local repo over HTTP (apt runs *inside* the
chroot, where a host `file://` path does not resolve), installs build-deps,
fetches source, runs `dpkg-buildpackage -b`, and publishes the output with the
`sid-host` chroot's own `reprepro` so the next package can build against it.
Proven end-to-end on `zip 3.0-16`.

Two things it encodes that cost real time to learn:

* **Build profiles first.** Twice a dependency that looked like it had to be
  built was optional behind a profile — `libcap-ng`'s bluez dep behind
  `pkg.libcap-ng.noutils`, and `libselinux`'s *entire* ruby + python3
  requirement behind `<!nopython> <!noruby>`. Checking for a profile is far
  cheaper than porting a language runtime. `libselinux` goes from unbuildable
  to a 7-package job on that basis alone.
* **`DEB_BUILD_OPTIONS` is space-separated; `DEB_BUILD_PROFILES` is
  comma-joined only when passed to apt as `-P`.** They are not
  interchangeable. Comma-joining the former makes dpkg discard the whole
  string as one bad flag:

  ```
  dpkg-buildpackage: warning: invalid flag in DEB_BUILD_OPTIONS:
      nocheck,noddebs,parallel=1
  ```

  which silently un-sets `nocheck`, so testsuites run anyway. That is how the
  bug was found, not by reading the manual.

### gcc has no `TARGET_LIBC_PROVIDES_SSP`

The bootstrapped gcc 16 was configured without it, so `-fstack-protector-strong`
compiles but cannot link: `cannot find -lssp`. Debian's mips64 gcc would
normally get the guard from glibc.

Worked around with an empty `libssp.a` in
`/usr/lib/gcc/mips64-linux-gnuabi64/16/`. This is safe *specifically* because
glibc does provide the symbols — verified by checking the linked binary still
carries live guard references (`__stack_chk` refs: 2), i.e. the protector is
real and only the redundant library was missing. It is still a build-host
workaround, not a fix: a rebuilt gcc should set the define.

### iproute2 and busybox: BUILT

All 12 chain packages built with `build-iproute2-chain.sh`. `iproute2 7.2.0-1`
ships 21 binaries, every one `ELF 64-bit MSB pie executable, MIPS, MIPS64
rel2`. Verified by running them, not by listing files:

    ip -V       ip utility, iproute2-7.2.0, libbpf 1.7.0
    ss -V       ss utility, iproute2-7.2.0
    tc -V       tc utility, iproute2-7.2.0, libbpf 1.7.0
    bridge -V   bridge utility, 7.2.0
    busybox     BusyBox v1.38.0 (Debian 1:1.38.0-3) multi-call binary

`ip -o link` makes real netlink calls. `cproot` is now 84 packages / 277 MB
with `ip`, `ss`, `tc`, `bridge`, `busybox` and `/sbin/init`, all packages in
`ii` state and `dpkg --audit` clean. Archive: 596 -> 633 packages.

The chain was much shorter than its dependency lists imply, because 30 of
iproute2's 44 transitive build-deps were already built. `libelf-dev` being
present removes `elfutils`, which was the only consumer of `gawk`, which was
the only consumer of `locales-all` -- a glibc rebuild avoided by measuring
rather than assuming.

#### Use apt, not `dpkg -i`

`dpkg -i iproute2...deb` leaves the package in state `iU` -- unpacked,
unconfigured -- because iproute2 Depends on `libcap2-bin` and dpkg does not
fetch dependencies. All 11 runtime deps were already in the repo. This looks
like a broken package and is not.

#### busybox provides no `ip` on its own

Debian's `busybox` package installs ONLY `/usr/bin/busybox`: `busybox.install`
lists the binary, a man page and two initramfs hooks, and the only `.links`
files in the source belong to `busybox-syslogd`, `udhcpc` and `udhcpd`. There
are no applet symlinks.

The obvious remedy, `busybox --install -s`, would be **actively harmful on this
root** -- it symlinks all ~300 applets and would shadow the real coreutils,
util-linux, findutils and login binaries a Debian root already has. iproute2
supplies the real `ip`, so busybox stays a fallback invoked as `busybox ip`.

### CPython 3.14.7: BUILT, and running on the silicon

Upstream CPython, not the Debian package. `build-cpython-mips64.sh`.

    version    3.14.7  (main, Sep 9 2026) [GCC 16.2.0]
    platform   linux-mips64      machine  mips64
    byteorder  big               maxsize  9223372036854775807
    ssl        OpenSSL 3.6.4     sqlite3  3.53.4     zlib 1.3.2

Verified on a Cavium Octeon III V0.3 with **no binfmt registrations and no
qemu binary on the machine** -- so this is silicon, not emulation:

    struct   native == big-endian: True
    sqlite   round-trip sum: 6
    threads  [0, 1, 4, 9, 16, 25, 36, 49]
    decimal  0.1428571428571428571428571429
    float    0.30000000000000004

`_decimal` and IEEE-754 float being exactly right is worth stating: both are
classic big-endian porting casualties.

**The FFN agents import and RUN under it**, not merely compile -- `ffn_bcmd`
executed its module-level code and produced its own diagnostic
(`### MISSING bcm.user: ...`), `ffn_cfgagent` imported, and `ffnrun` exited 2
as an argparse script should. py_compile only parses; importing is what
exercises a port.

#### Why upstream and not src:python3.14

Its Build-Depends are a wall, and the expensive entries are hard-required
rather than profile-guarded: `locales-all` (a glibc REBUILD), `tk-dev`
(tcl + tk + X11), `systemtap-sdt-dev`, plus `python3:any` -- python needs
python to build. Upstream needs only OpenSSL, zlib, ffi, readline, sqlite3,
expat, bz2, lzma, ncurses, gdbm and uuid, and **all sixteen were already built
in this port**. One build instead of a chain, exactly as with OpenSSH.

Extract the pristine `.orig.tar.xz` rather than using `apt-get source`, which
applies Debian's patch series -- those touch `configure.ac`, making upstream's
shipped `configure` older than its input so it refuses to run. For OpenSSH the
answer was `autoreconf`; CPython pins specific autoconf/aclocal versions and
regenerating with the wrong ones fails less obviously.

`configure` reports `build == host == mips64-unknown-linux-gnuabi64`, i.e. a
NATIVE build under emulation, so none of the cross-compilation machinery
(`--with-build-python`, host/target interpreter mismatch) is involved.

#### Three traps in getting it into a root

**Runtime libraries are a separate step from build ones.** The build root had
the `-dev` packages; the target root had neither those nor their runtime
counterparts, so python started and then died on the first C extension:
`ImportError: libsqlite3.so.0`. Checking only the python binary misses most of
it -- every extension in `lib-dynload` links its own libraries. Seven sonames
were missing; resolve them by asking dpkg which package owns each, because two
of the six are t64 variants (`libgdbm6t64`, `libreadline8t64`) and
`libncursesw6` supplies both `libncursesw` and `libpanelw`.

**A soname-prefix glob misses sqlite.** `libsqlite3.so.0*` matches the symlink
but NOT the real file, which is `libsqlite3.so.3.53.4` -- soname major 0,
file version 3.53.4. Every other library here has a filename that starts with
its soname, so the glob works for them and silently fails for this one.

**Files dropped in by tar are invisible to the loader.** Debian's loader reads
`/etc/ld.so.cache`, which tar does not update, so the libraries were present
and still "not found". Run `ldconfig` -- and it has to run on the target, since
it is a mips64 binary. Installing via apt avoids this because the ldconfig
trigger runs.

### Known-good, and the honest gaps

Built and validated: upstream **OpenSSH 10.5p1** (`sshd`, `ssh`, `ssh-keygen`,
`scp` in `ssh-build/`), `sshd -t` passing *natively on the CN73XX*.

`iproute2` and `busybox` are now built and installed in `cproot` (above).
busybox fails two of its own tests here — "printf understands %s" and
"printf handles positive numbers for %f" — which `nocheck` skips. A
big-endian `%f` result deserves a look on real hardware, where it would
actually mean something.

**The remaining blocker for booting the CP on this root is `nfs-common`, not
`ip`.** The CP roots over NFS, so without it the root cannot mount itself.

## Endianness: CONFIRMED

This was the one risk worth checking before anything else, because the failure
mode is a complete distribution that will not run on the hardware — discovered
at the end of a multi-hour build rather than the start.

```
$ mips64-linux-gnuabi64-ld --print-output-format
elf64-tradbigmips
```

`elf64-tradbigmips` is the **default** output format, and it is the same format
`objdump` reports for the vendor's own libraries (`libports.so`:
`file format elf64-tradbigmips`). The cross binutils also lists
`elf64-tradlittlemips` as *available* — that is normal, BFD supports both; what
matters is which one is default.

Checked as soon as stage 1 produced a linker, ~6 minutes in, rather than waiting
for stage 7 as originally planned.

## Stages

| # | stage | state |
|---|---|---|
| 1 | binutils | **done** — `binutils-mips64-linux-gnuabi64` |
| 2 | gcc stage1 (C only, no libc) | **done** |
| 3 | linux headers | **done** — `linux-libc-dev-mips64-cross` |
| 4 | glibc stage1 (headers + crt) | **blocked, then fixed** — see below |
| 5 | gcc stage2 | **done** — `gcc-16-mips64-linux-gnuabi64`, `cpp-16`, `libgcc-16-dev` |
| 6 | glibc | **done, complete package set** — see below |
| 7 | gcc stage3 | **done** — native gcc 16.2.0, big-endian `elf64-tradbigmips` |
| 8 | base system | **done enough to self-host** — 596 mips64 binaries; `build-essential` + `debhelper` installable |
| 9 | builds its own packages | **done** — see `build-deb-mips64.sh` above |

Ten packages built before the first stall, including the first genuinely
target-architecture one: `binutils-for-host_2.47-4_mips64.deb`. The run
eventually reached 596 distinct mips64 binaries.

## glibc: the full package set, seven mips64 packages

```
binutils-for-host_2.47-4_mips64.deb
libc6_2.43-5_mips64.deb
libc6-dev_2.43-5_mips64.deb
libc6-dbg_2.43-5_mips64.deb
libc-bin_2.43-5_mips64.deb
libc-dev-bin_2.43-5_mips64.deb
libc-gconv-modules-extra_2.43-5_mips64.deb
```

That is the hard part of a Debian bootstrap finished. It took **five build
cycles**, and the breakdown is worth keeping honest: two were genuine Debian
packaging bugs (below), and two were mine — I reasoned about make globs twice
instead of reading the `DH_VERBOSE=1` output that showed the answer directly.
The fifth was the `--ignore-missing-info` case.

## glibc built — and it is the right architecture

The decisive check, on a real target binary rather than the linker's default:

```
usr/lib/mips64-linux-gnuabi64/libc.so.6
  ELF 64-bit MSB shared object, MIPS, MIPS64 rel2, interpreter /lib64/ld.so
usr/lib/mips64-linux-gnuabi64/ld.so.1
  ELF 64-bit MSB shared object, MIPS, MIPS64 rel2
```

**MSB** is big-endian. This is the same signature as the hardware's own
binaries — `/opt/dproot/bin/busybox: ELF 64-bit MSB executable, MIPS, MIPS64
rel2`, and the vendor libraries' `elf64-tradbigmips` — so a Debian C library now
exists for this platform's ABI.

A toolchain that *targets* an architecture proves less than a C library built
*for* it: the toolchain check (`ld --print-output-format`) confirms intent, this
confirms output.

## The first real porting bug: glibc's stamp path

glibc 2.43-5 failed with

```
make: *** No rule to make target '.../stamp-dir//build_libc',
         needed by '.../stamp-dir/build_C.utf8'.  Stop.
```

The double slash is the whole diagnosis:

```
debian/rules:42               stamp := $(CURDIR)/stamp-dir/    <- ends in /
debian/rules.d/build.mk:375   $(stamp)build_C.utf8:      $(stamp)/build_libc
debian/rules.d/build.mk:379   $(stamp)build_locales-all: $(stamp)/build_libc
```

Every other reference concatenates directly (`$(stamp)build_foo`) because
`$(stamp)` already carries the separator. Exactly two lines insert a second
one, and **make treats `a//b` and `a/b` as different target names** — so the
prerequisite matches no rule.

**Why Debian never trips over it.** When the stamp *file* already exists, the
filesystem collapses the double slash, make finds it, and no rule is needed. It
only fails when make must actually *build* that prerequisite — which is exactly
the staged cross-bootstrap case, where the libc pass has not run yet. That makes
it a bug only a new architecture can hit, and worth reporting upstream.

Fixed by a sed inside rebootstrap's own `patch_glibc()` hook, so it survives
rebootstrap re-unpacking the source each run. `mips64.mk` was the obvious
suspect and was *not* the problem — it exists and is structurally identical to
`mips64el.mk`, differing only in endianness-specific names.

Host-side scaffolding builds first and is easy to mistake for progress on the
target. Read the architecture suffix, not the name:

```
build-essential_12.12+rebootstrap1_amd64.deb        host tooling
libc6-dev_2.43-5_amd64.deb                          host tooling
binutils-mips64-linux-gnuabi64_2.47-4_amd64.deb     a CROSS tool, runs on amd64
cpp-16-mips64-linux-gnuabi64_16.2.0-2_amd64.deb     ditto
gcc-16-mips64-linux-gnuabi64_16.2.0-2_amd64.deb     ditto

binutils-for-host_2.47-4_mips64.deb                 <- the target architecture
```

A package *named* `-mips64-linux-gnuabi64` is usually a cross tool built **for
amd64**; only the `_mips64.deb` suffix means it runs on the target.

## Watching it

`DH_VERBOSE=1` is set by rebootstrap, so the log grows fast — 11,785 lines in
the first four minutes. Do not grep it for `rebootstrap-error`: the log echoes
the script's own source at the top, so those matches are `echo` statements with
literal `$errcode`/`$pkg` in them, not events. A filter that matched only those
reported three "errors" during a completely healthy build.

Useful signals instead:

```
dpkg-deb: building package '<name>'      one .deb produced -- a real milestone
dpkg-buildpackage: error                 with EXPANDED values, not $vars
```

## Not on the critical path

OpenWrt 24.10.4 `mips64_octeonplus` continues to serve both planes over the
nested NFS (`../NFS-LAYERING.md`), so the CP and DP have working userspaces
regardless of how this port goes. Nothing waits on it.
