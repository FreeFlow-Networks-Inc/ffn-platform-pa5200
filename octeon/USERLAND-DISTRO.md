# PA-5200 operating system policy

The management plane uses Ubuntu amd64. Both OCTEON planes use source-built
Debian MIPS64 **big-endian**, n64 ABI, glibc and systemd. `mips64el` packages
are not compatible. Debian's mainstream package archive is not a source of
ready-made packages for this big-endian port; maintain a pinned source-built
package repository for it.

The release policy is in `images/os-policy.json`. OCTEON kernels must be
Linux 6.18 or newer, with reviewed board/transport patches and modules built
from the same pinned source. The Ubuntu MP retains its supported Ubuntu kernel;
the OCTEON minimum does not apply to the management plane.

OpenWrt roots, musl toolchains, CentOS/RPM roots, obsolete vendor kernels and
mixed Buildroot initramfs images are retired build inputs. There is no
cross-distribution recovery fallback in a release image. Recovery must use a
previously qualified image from the same OS family.

Use `images/build_images.py` and the protected image workflow. The builder
checks distro metadata, Debian package architecture and runtime ownership,
ELF endianness, compiler target, kernel version and machine identity. Checks
are necessary but do not replace source/package provenance or hardware testing.

BCM, FE100, PHY, PCIe and CVMX hardware interfaces remain necessary. Retain
hardware source, register maps and firmware provenance separately from an OS
sysroot. A recovered vendor root or compatibility chroot must never be added
to a Debian release to make a missing driver work. Port and package the required
native integration, then qualify it on hardware.

Historical bring-up documents describe earlier experiments, not the release
build procedure. Existing live installations are not migrated by changing this
policy. A new release still needs clean packaged Python/systemd/networking,
Debian-built transport binaries, native BCM/FE100 integration and CP/DP boot,
agent, LACP, policy and NAT qualification before activation.
