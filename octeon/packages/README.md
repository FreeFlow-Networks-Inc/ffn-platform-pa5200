# Debian OCTEON packages

Run the transport builder **inside a Debian build environment** with the
MIPS64 big-endian GNU cross compiler and glibc development packages. It does
not install a package, publish an archive or contact a processor.

```sh
python3 octeon/packages/build_transport.py --platform /path/to/platform \
  --out /path/to/new-output-directory --version VERSION \
  --source-date-epoch EPOCH
```

Use a native Debian version (no hyphen/revision), and the reviewed source's
Unix commit timestamp for EPOCH. The output directory must not already exist.
The build produces a `.deb`, Debian source `.dsc`/tarball, `.buildinfo`,
`.changes`, build log and a manifest of source and artifact hashes.

The package installs `ffn_pcnetd`, `ffn_dpnetd`, the `ffn_dpagent2` recovery
mailbox, `ffn_nfsmount` and `ffn-systemd-handoff` in `/usr/libexec/ffn/`.
All are static MIPS64 big-endian executables. A network-root transport must
be copied into RAM before it serves the filesystem holding its own executable.
The MP owns that staging and the role-specific service start/stop sequence.
This package contains no maintainer scripts, service activation, interface
configuration, credentials or hardware reset commands.

`Built-Using` records the exact glibc and GCC source versions linked into the
executables. The package's own source archive does **not** contain those
external sources; include their matching sources and notices in a complete
release source bundle. The candidate remains unpublished and unqualified.

## Runtime inventory

```sh
python3 octeon/packages/runtime_inventory.py --role cp \
  --index /path/to/Packages --rootfs /path/to/cp-root
python3 octeon/packages/runtime_inventory.py --role dp \
  --index /path/to/Packages --index /path/to/additional/Packages
```

The role requirements are reviewed in `runtime-requirements.json`. The report
distinguishes available packages from configured installed packages and ignores
little-endian packages. Exit 1 indicates a gap. This is a package inventory,
not an APT dependency solver or hardware readiness check.
The image builder requires the same role package set before compiling a kernel.

Python, SSH and NFS must be built as genuine Debian source packages with their
dependencies. Do not wrap imported loose executables or forge dpkg status to
pass image checks. Native BCM/FE100 service integration and CP/DP boot testing
remain separate qualification requirements.

## Debian runtime packaging adjustments

`prepare_runtime_source.py PACKAGE EXTRACTED_SOURCE` records these adjustments
as a `+ffn1` Debian revision before cross-building. Keep the resulting `.dsc`,
original tarball and Debian patch archive with the image's corresponding sources.

- OpenSSH retains its other hardening but disables GCC's zero-call-used-registers
  option on MIPS64 because GCC 16 fails during RTL compilation with that option.
- Python retains its runtime modules but omits external DTrace and Valgrind
  instrumentation on MIPS64.
- NFS retains NFSv3/v4 and NSS/GSS mapping; the optional LDAP idmapper and pNFS
  block-layout daemon are omitted from this internal boot-server build.
- nftables uses native Python packaging tools for its pure ctypes binding while
  nft and libnftables are compiled for MIPS64.

Cross-builds use `nocheck`; execute representative target programs under MIPS64
emulation and qualify the resulting images on hardware before promotion.
