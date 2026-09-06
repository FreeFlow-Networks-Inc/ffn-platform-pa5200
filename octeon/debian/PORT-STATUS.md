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
| 4 | glibc stage1 (headers + crt) | |
| 5 | gcc stage2 | |
| 6 | glibc | |
| 7 | gcc stage3 | |
| 8 | base system (~1000 source packages) | |

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
