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

### Known-good, and the honest gaps

Built and validated: upstream **OpenSSH 10.5p1** (`sshd`, `ssh`, `ssh-keygen`,
`scp` in `ssh-build/`), `sshd -t` passing *natively on the CN73XX*.

Not yet in any root: `ip`/`ifconfig`. `iproute2` is a ~12-package chain with no
profile escapes (`bison` needs `help2man`; `gawk` needs `bison` and
`locales-all`; `libmnl` needs `doxygen` + `graphviz`; `iptables` needs
`libmnl-dev` and `libnetfilter-conntrack-dev`; `elfutils` needs `gawk` and
`bison`). busybox is the cheap substitute for `ip`/`ifconfig` and builds, but
fails two of its own tests here — "printf understands %s" and "printf handles
positive numbers for %f". Those are skipped under `nocheck` and deserve a look
on real hardware, where a big-endian `%f` result would actually mean something.

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
