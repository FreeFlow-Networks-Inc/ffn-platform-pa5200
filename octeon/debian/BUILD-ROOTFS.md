# Debian sid MIPS64 big-endian image builder

The build host is `stephen@192.168.47.129` (`reverse-engine`). The working
directory is `/mnt/clones/debian-mips64`; the amd64 Debian sid build chroot is
`sid-host`. Target architecture is **mips64**, GNU triplet
**mips64-linux-gnuabi64**, big-endian **n64**, MIPS64r2 instruction set.

This is a source-built Debian port. The root filesystem is separate from the
board's kernel, device tree, bootloader and storage layout. For the PA-5220,
use the board-specific kernel/boot workflow in the parent Octeon directory.

As of 2026-09-08, the 81-package base image has passed all runtime checks,
including authenticated HTTPS APT updates. It is available at
`/mnt/clones/debian-mips64/images/debian-sid-mips64-be-base.tar` (147 MiB).
Native GCC and the buildd image are still in progress; do not treat the
developer image as ready until its verification log reports success.

## Resume on the VM

Copy these helpers into `/mnt/clones/debian-mips64`, then run:

```sh
sudo sh /mnt/clones/debian-mips64/setup-port-completion.sh
sudo systemd-run --unit=ffn-debian-port --collect \
  /bin/sh /mnt/clones/debian-mips64/resume-port.sh
```

The systemd job survives SSH disconnection. `resume-port.sh` holds a file lock,
checks the chroot mounts, restores the MIPS64 QEMU registration, and reuses
rebootstrap's completed-stage stamps. Run setup while no build is active.

```sh
systemctl status ffn-debian-port
tail -f "$(cat /mnt/clones/debian-mips64/logs/complete-port.latest)"
cat /mnt/clones/debian-mips64/logs/complete-port.exit
```

The initial completion run uses `ffn-debian-port-completion.service` after its
one-time handoff from the first Perl build. Check that unit as well when
inspecting the 2026-09-08 run.

The exit file is `running` during the job, then its numeric exit status. A zero
requires successful image assembly and runtime checks, not merely reaching the
end of upstream rebootstrap's script. Earlier bootstrap logs and packages are
preserved. A failed package's source directory is retained for diagnosis; do
not delete completed-stage stamps to retry an unrelated failed package.

## Completion stages

* Perl: cross-compile Debian's package, using QEMU to run actual big-endian
  configuration probes. No little-endian configuration is substituted.
* APT: correct its dependency to gpgv in a local `3.3.3+ffn1` package. The
  original packaging inferred sqv availability from the amd64 build host;
  the existing binary already contains and selects the gpgv fallback when
  sqv is absent. The original package is archived and executable files are
  preserved. `fix-apt-metadata.py` records the repeatable transformation.
* Native GCC: cross-build C/C++ compilers which execute on MIPS64. Keep the
  existing amd64 cross-compiler packages when importing the native packages.
* Root filesystems: `base` includes systemd and basic administration tools;
  `buildd` additionally includes native GCC/G++, Perl, build-essential and
  debhelper. Dependencies are resolved by mmdebstrap.

`complete-port.sh` is sourced before upstream rebootstrap's final installability
report. `install-port-extension.py` preserves the pre-extension bootstrap script.
The host preparation registers only ELF64 big-endian MIPS executables with the
static QEMU interpreter and builds a real `arch-test` helper for this architecture.

## Outputs and checks

Successful output goes to `/mnt/clones/debian-mips64/images/`:

* `debian-sid-mips64-be-base.tar` and `debian-sid-mips64-be-buildd.tar`
* SHA-256 files alongside each archive
* extracted `rootfs-base` and `rootfs-buildd` directories
* `verify-base.log` and `verify-buildd.log`

The checks inspect ELF class, endianness and MIPS machine ID, check the dpkg
architecture and package configuration, and execute Perl and systemd's version
command. The buildd check compiles and runs C and C++ programs using the native
MIPS64 compilers under QEMU, including a runtime check for 64-bit pointers and
big-endian memory representation. These checks do not constitute a hardware
boot test.

The buildd check also builds a small `ffn-toolchain-smoke_1.0_mips64.deb` with
`dpkg-buildpackage` and debhelper. The extracted buildd filesystem retains its
source and resulting package under `/root/ffn-smoke`.

The package repository remains at `sid-host/tmp/repo`. The image's local APT
source references `/tmp/repo`: mount/copy this repository there for local use,
or change that source to your package server before deployment. Architecture-all
packages use Debian's signed sid archive. Configure users, networking, board
kernel and boot storage for the destination before booting it.

To assemble a new image manually inside the build chroot:

```sh
sudo chroot /mnt/clones/debian-mips64/sid-host \
  sh /root/rebootstrap/build-rootfs.sh base
```

The image script refuses to overwrite an existing archive. Preserve previous
images before rebuilding them.
