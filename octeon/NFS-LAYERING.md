# Nested NFS: how each plane gets a filesystem

```
MP NFS  ->  CP OS + filesystem  ->  CP NFS  ->  DP OS + filesystem
```

The MP exports the CP's root. The DP's root lives *inside* that tree. The CP
re-exports it. Each plane roots over the network from the plane above it, and
nothing has to be pushed anywhere.

## Why this shape, and not file transfer

Every earlier route to the DP went through the PCIe mailbox: `ffn_dpstage.py`
writes a megabyte into a staging window, then `ffn-dpsh` has the DP read it out
of `/dev/mem` and append. That transport is a single shared `/bin/sh` over a
mailbox, and it does not survive bulk data — a 3.4 MB image would not arrive
intact (every individual chunk verified and the whole-file hash did not), and
`sha256sum` piped in that shell returns nothing while `ls` in the same command
works.

This layering deletes the problem rather than working around it. The DP's
filesystem is authored on the **MP**:

```
/opt/ffn-cproot-owrt/opt/dproot      on the MP
        = /opt/dproot                on the CP   (its root IS that directory)
        = /                          on the DP   (mounted from the CP)
```

Write a file on the MP and it is already on the CP. No staging, no chunking, no
hash reconciliation.

## Layer 1: MP -> CP  (already in place)

```
127.1.1.1:/opt/ffn-cproot-owrt / nfs rw,vers=3,nolock,proto=tcp
```

The MP's `/etc/exports` carries `/opt/ffn-cproot-owrt 127.1.0.0/16(rw,sync,
no_root_squash,no_subtree_check)`. Addresses are in 127/8 because the CP reaches
the world only across the PCIe virtual-ethernet link — see
`ffn-owrt-mirror.conf` for why that shapes the package feed too.

## Layer 2: CP -> DP  (`ffn-cp-nfsd.sh`)

Three things had to be built, and each failed in a way that named something
else.

**nfsd.ko is ours.** `CONFIG_NFSD` was unset in the CP kernel — but every
dependency was already built in (`EXPORTFS`, `GRACE_PERIOD`, `LOCKD`,
`LOCKD_V4`, `SUNRPC`, `NFS_FS`), because the CP is already an NFS *client*. So
it builds as one module against the running kernel, vermagic `6.18.49-dirty`,
the same way `i2c-dev` did. No reboot.

**opkg cannot supply it.** `nfs-kernel-server` depends on `kmod-fs-nfsd`,
`kmod-fs-nfs` and `kmod-fs-nfs-v4`, built for OpenWrt's own kernel. opkg
rejects the package as *architecture-incompatible* before dependency
resolution, so even `--nodeps` fails. The userspace binaries (`exportfs`,
`rpc.nfsd`, `rpc.mountd`, `rpc.statd`) are extracted from the `.ipk` instead;
the module is ours and the dependency is genuinely satisfied, just not through
opkg's view of the world.

**`fsid=` is mandatory, not tuning.** `/opt/dproot` sits on an NFS mount, so
this is an NFS **re-export**. nfsd cannot derive a filesystem identifier for a
filesystem it does not own, and the export is refused outright without one.
Linux has supported re-export since 5.11 and this kernel is 6.18, so only the
fsid was missing. Keep the number stable: changing it invalidates every
client's file handles, which surfaces as stale-handle errors rather than as a
configuration change.

## Verified

The MP reaches the CP over ffnnet0, so mounting the CP's re-export from the MP
exercises both layers at once — the MP reading its own bytes back down through
two NFS hops:

```
$ mount -t nfs -o nolock,vers=3,ro 127.1.1.2:/opt/dproot /mnt/dptest
MOUNTED via CP
bin dev etc lib lib64 mnt overlay proc rom root sbin sys tmp usr var www
/mnt/dptest/bin/busybox: ELF 64-bit MSB executable, MIPS, MIPS64 rel2
DISTRIB_ID='OpenWrt'
DISTRIB_RELEASE='24.10.4'
```

Server state on the CP: 8 threads, `+3` (NFSv3 — which is what both the CP's own
root and the DP use), listening on 2049.

## The DP root itself

OpenWrt 24.10.4 `mips64_octeonplus`, 19 MB, taken from the octeon target's
`squashfs-sysupgrade.tar` (OpenWrt publishes no plain rootfs tarball for this
target; the `root` member of that tar *is* a squashfs root). Same distribution,
release and ABI as the CP — one userland across both planes, and `opkg` works
against the same mirror once the DP has a route.

`busybox` in it is `ELF 64-bit MSB MIPS64 rel2`, so the architecture is right.
The squashfs container is little-endian, which is not a mismatch: squashfs 4.0 is
little-endian by specification and the kernel byte-swaps.

## What is still missing

**A network path between CP and DP.** Layer 2 serves; nothing has mounted it
from the DP yet, because the DP has no address. `ffn_dpnet` (CP<->DP virtual
ethernet over PCIe, 127.1.2.x) is the remaining piece, and it is the
prerequisite for the DP side of this — not an optimisation of it.

Once the DP has an address, it needs `root=/dev/nfs nfsroot=127.1.2.1:/opt/dproot`
plus `ip=` on its kernel command line, or a mount from its initramfs. Note
`nolock` on every mount here: the CP's own root uses it, and lockd across two
nested re-exports is not something to inherit by accident.
