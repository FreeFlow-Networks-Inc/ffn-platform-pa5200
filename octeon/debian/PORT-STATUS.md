# Debian mips64 (big-endian) port — status

Running. Started 2026-09-06 on the RE VM (8 cores, 30 GB RAM, 149 GB free on
`/mnt/clones`), inside a Debian sid amd64 chroot, driven by Debian's own
`rebootstrap`. See `ffn-debian-mips64-bootstrap.sh` for the recipe and
`../USERLAND-DISTRO.md` for why this is a port and not an install.

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
| 2 | gcc stage1 (C only, no libc) | in progress |
| 3 | linux headers | **done** — `linux-libc-dev-mips64-cross` |
| 4 | glibc stage1 (headers + crt) | **blocked, then fixed** — see below |
| 5 | gcc stage2 | **done** — `gcc-16-mips64-linux-gnuabi64`, `cpp-16`, `libgcc-16-dev` |
| 6 | glibc | retrying with the fix |
| 7 | gcc stage3 | |
| 8 | base system (~1000 source packages) | |

Ten packages built before the stall, including the first genuinely
target-architecture one: `binutils-for-host_2.47-4_mips64.deb`.

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

Host-side scaffolding built first and is easy to mistake for progress on the
target: `build-essential`, `libc6-dev` and `binutils-for-host` are all **amd64**
packages for the cross build. The first genuinely target-architecture artifact
was `binutils-mips64-linux-gnuabi64`.

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
