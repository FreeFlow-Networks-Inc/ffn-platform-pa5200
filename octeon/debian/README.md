# Debian OCTEON port

Production image construction lives in `../images/`. Mixed-root construction
scripts (`build-systemd-boot.sh`, `deploy-plane-candidates.sh` and
`install-debian-boot.sh`) have been removed: they copied foreign initramfs,
compatibility roots or appliance credentials. Build reviewed package seeds
and provision machine identity through the MP instead.

Other scripts here record hardware bring-up and package porting work. They
are not a release installation procedure. In particular, compatibility-root
BCM/FE100/DP boot helpers cannot be included in a pure Debian image; native
Debian equivalents must be packaged and hardware-qualified first. The release
overlay includes the observation agents, not these compatibility services.
