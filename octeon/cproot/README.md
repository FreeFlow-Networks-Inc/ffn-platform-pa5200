# Control-plane root filesystem

The CP release root is Debian MIPS64 big-endian with glibc and systemd.
The MP is Ubuntu amd64. See `../USERLAND-DISTRO.md` and `../images/README.md`.

OpenWrt ImageBuilder staging and its opkg mirror configuration have been
removed. Do not reconstruct a CP root from a recovered vendor sysroot or add
a compatibility chroot. The DP must use the same Debian architecture and
kernel policy, with its own role-specific package/agent configuration.

Build clean, identity-free rootfs and initramfs seeds from reviewed Debian
packages, pin their hashes and corresponding sources in the runner profile,
and use the CP/DP image workflow. Python and systemd must be Debian-packaged;
loose binaries copied over a bootstrap root do not qualify.

NFS transport/export settings are provisioned by the MP. Legacy manual
bring-up helpers in this directory are not release image construction steps.
The release workflow does not invoke them or activate images on an appliance.
