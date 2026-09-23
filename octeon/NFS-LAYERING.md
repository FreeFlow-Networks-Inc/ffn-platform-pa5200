> Historical bring-up record. Distro choices and compatibility-root instructions
> below are retired. Use `octeon/images/README.md` and `octeon/USERLAND-DISTRO.md`
> for the Debian CP/DP and Ubuntu MP release policy.

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

The CP now roots on **Debian**, with the old OpenWrt root mounted alongside it:

```
127.1.1.1:/opt/ffn-cproot-debian-20260909  /              nfs vers=3,nolock
127.1.1.1:/opt/ffn-cproot-owrt             /opt/ffn-compat nfs vers=3,nolock
```

The MP's `/etc/exports` carries both with `127.1.0.0/16(rw,sync,
no_root_squash,no_subtree_check)`. Addresses are in 127/8 because the CP reaches
the world only across the PCIe virtual-ethernet link — see
`ffn-owrt-mirror.conf` for why that shapes the package feed too.

### What the move to Debian changed, and what it did not

**The DP's root did not move.** It is still authored at
`/opt/ffn-cproot-owrt/opt/dproot` on the MP and still served as `/opt/dproot`,
so the identity this whole design exists for is intact — the difference is only
that the CP reaches it through `/opt/ffn-compat` rather than through its own
`/`. The daemons that serve it are chrooted there, so the path they resolve is
unchanged. Nothing had to be copied.

**nfsd is built in, not a module.** `CONFIG_NFSD` was unset in the OpenWrt CP's
kernel and the module had to be built (below); the Debian CP kernel has it
compiled in and `/proc/filesystems` lists `nodev nfsd` at boot. The `insmod`
branch of `ffn-cp-nfsd.sh` is a no-op there and correctly skips itself.

**The NFS userland is used in place, not ported.** `nfs-utils` does not build
for mips64 here — it wants `libdevmapper-dev`, `libnl-3-dev`,
`libnl-genl-3-dev` and `rpcsvc-proto`, none of which are in the CP's 91-package
root. It does not need to: the OpenWrt binaries extracted earlier are already on
`/opt/ffn-compat`, they are musl-linked against a libc that is right there, and
they run unmodified under `chroot /opt/ffn-compat`. `readlink /proc/<pid>/root`
on the live server shows exactly that:

```
785  rpcbind      root=/opt/ffn-compat
820  rpc.mountd   root=/opt/ffn-compat
799..806 nfsd     root=/          (kernel threads; they have no userland root)
```

That the daemons are chrooted is invisible to clients: `showmount -e 127.1.1.2`
lists `/opt/dproot` and `/opt/dproot-owrt`, and both mount.

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

**Debian GNU/Linux forky/sid, mips64 big-endian, 1.1 GB** — the same
distribution the CP roots on, so one userland now covers both OCTEONs. Proven
by execution on the DP, not by reading a mount table:

```
/ is 127.1.2.1:/opt/dproot                      (nfs, vers=3, nolock)
PRETTY_NAME="Debian GNU/Linux forky/sid"
chroot /proc/1/root /bin/uname -srm   Linux 6.18.49 mips64
chroot /proc/1/root /bin/ls /usr/bin  303 binaries
chroot /proc/1/root python3 -c ...    3.14.7 mips64 big
```

A real interpreter on the dataplane is what the swap was for. Note that
`/sbin/init` in this tree is a symlink to systemd and **is never executed**:
pid 1 on the DP stays `ffn_init` from the initramfs, which does `MS_MOVE` +
`chroot` and keeps the mailbox agent it already forked. The Debian tree is a
filesystem, not a boot.

### Swapping the root: rename, not a bind mount

The DP's initramfs hardcodes `EXPORT=/opt/dproot`, so a new tree has to be
reachable under that name. A bind mount over it works and needs no rebuild, but
it does not survive a CP reboot — the CP would silently revert to serving the
old tree while every path and document still said otherwise. A rename is
persistent, is honest (one name, one tree), and is undone by renaming back.

**`fsid=` follows the ROLE, not the tree.** 7 has always meant "the DP's root"
and the initramfs asks for `/opt/dproot`, so 7 stays with that path across
swaps and the displaced tree takes 8. The stale file handles this creates never
matter because the DP is reset immediately afterwards.

The previous root is **kept, never deleted** — `/opt/dproot-owrt`, `fsid=8`. It
is the tree the DP is known to boot on, which is what makes the swap
reversible:

```sh
mv .../opt/dproot .../opt/dproot-debian && mv .../opt/dproot-owrt .../opt/dproot
# restore exports, exportfs -ra in the chroot, re-run dp-nfsboot-debian.sh
```

### The previous root, for reference

OpenWrt 24.10.4 `mips64_octeonplus`, 19 MB, taken from the octeon target's
`squashfs-sysupgrade.tar` (OpenWrt publishes no plain rootfs tarball for this
target; the `root` member of that tar *is* a squashfs root). Same distribution,
release and ABI as the CP — one userland across both planes, and `opkg` works
against the same mirror once the DP has a route.

`busybox` in it is `ELF 64-bit MSB MIPS64 rel2`, so the architecture is right.
The squashfs container is little-endian, which is not a mismatch: squashfs 4.0 is
little-endian by specification and the kernel byte-swaps.

## Layer 3: the DP mounts it  (`../dproot/ffn-dp-nfsroot.sh`)

`../dpnet2/ffn-dpnet-up6.sh` brings up the CP↔DP virtual ethernet
(127.1.2.1 ↔ 127.1.2.2 — pure userspace, one static binary serving both ends
via `--role cp` / `--role dp`, no kernel module). Then the DP mounts and runs:

```
dp-nfsroot: CP<->DP link up
dp-nfsroot: /opt/dproot is exported
dp-nfsroot: mounted 127.1.2.1:/opt/dproot on the DP at /mnt/dproot
dp-nfsroot: chroot works: Linux mips64
dp-nfsroot: release: 24.10.4
```

and inside it:

```
Linux (none) 6.18.49-00006-gf6d0533138e7-dirty SMP PREEMPT mips64 GNU/Linux
DISTRIB_ID='OpenWrt'   DISTRIB_RELEASE='24.10.4'
65 binaries in /usr/bin
```

That is the DP's own kernel running an OpenWrt userspace binary out of a
filesystem served by the CP, which is itself served by the MP.

### Two failures that name the wrong thing

**"not found" on a file that is plainly there.** Executing from the mount
without chroot gives `sh: /mnt/dproot/bin/busybox: not found`. The missing
thing is the ELF interpreter: OpenWrt links against
`/lib/ld-musl-mips64-sf.so.1`, and from the initramfs root that path resolves
into the initramfs, which has no musl. "not found" naming an existing file
almost always means the interpreter.

**opkg cannot take its lock** — `Could not create lock file
/var/lock/opkg.lock`, which reads like a permissions or read-only-root fault. A
squashfs root ships no runtime state and OpenWrt's preinit never runs in a
chroot, so the directory simply did not exist. The script creates the runtime
directories in the export, and bind-mounts the DP's own `/proc`, `/sys` and
`/dev` into the chroot — those are per-machine while the export is shared, and
without `/proc` anything reading `/proc/self` fails obscurely.

### chroot, not switch_root

The DP's kernel booted on the initramfs and the dataplane, agent and mailbox all
run from it — including `ffn-dpsh`, which is the only way the DP is reachable at
all. `switch_root` would discard that. chroot gives the full userspace to work
that wants it while leaving the control path intact.

A real `switch_root` belongs in the DP's boot sequence — `root=/dev/nfs
nfsroot=127.1.2.1:/opt/dproot` plus `ip=` on the kernel command line — not in a
live session.

`nolock` is on every mount here deliberately: the CP's own root uses it, and
lockd across two nested NFS re-exports is not something to inherit by accident.

## What is still missing

**A route from the DP to the MP's package mirror.** `opkg` inside the chroot
cannot reach `127.1.1.1:8080` until the DP has a default route via 127.1.2.1 and
the CP forwards. Until then packages are added on the MP, into
`/opt/ffn-cproot-owrt/opt/dproot`, where they appear on the DP immediately —
which is the same property that made this design worth building.

## The vendor master tree is chained the same way

`/opt/dpfs` is the vendor's tree, 2.2 GB, and the **MP holds the master**. It is
chained down the same MP → CP → DP path as the DP's root rather than being a
CP-only mount:

```
MP  /opt/dpfs                    master, exported rw to 127.1.0.0/16
CP  /opt/ffn-compat/tmp/dpfs     mounted ro   (= /tmp/dpfs inside the chroot)
     └─ re-exported ro, fsid=9
DP  /opt/dpfs                    mounted ro from 127.1.2.1:/tmp/dpfs
```

Verified through all three hops — the DP reads `bcm.user` at 196,484,446 bytes,
the same size the MP holds; `brdagent/cp` shows its five role plugins and
`brdagent/dp` its one; both vendor module trees (`3.10.87-oct2-dp`, `4.9.57`)
are visible.

**Read-only the whole way down, and that is not a limitation.** The CP already
mounts the master `ro`, so an `rw` re-export would be a lie the DP discovers as
`EROFS` on its first write. It also happens to be the policy — vendor firmware
is used in place and never modified — so the chain enforces what would otherwise
rely on everyone remembering.

**The export path is chroot-relative.** `rpc.mountd` runs inside
`/opt/ffn-compat`, so what it and its `/etc/exports` call `/tmp/dpfs` is
`/opt/ffn-compat/tmp/dpfs` on the CP, and the DP asks for
`127.1.2.1:/tmp/dpfs`. The name is the compat root's own — `dpboot8.sh`
hardcodes `VT=/tmp/dpfs`, so it cannot be tidied without breaking the DP boot.

**`fsid=9`, and the export was published singly.** `/tmp/dpfs` sits on an NFS
mount, so this is a re-export and nfsd refuses it without an id; 7 is the DP's
root and 8 the OpenWrt fallback. It was added with
`exportfs -o … 127.1.0.0/16:/tmp/dpfs` rather than `exportfs -ra`, deliberately:
the DP is *rooted* on fsid=7 at the time, and re-exporting everything is a
bigger blast radius than adding one entry for no benefit.

### Not yet persistent on the DP

The CP-side export is in the compat `/etc/exports` and survives a reboot. The
DP-side mount does not: the DP runs no init that reads `fstab` — pid 1 is
`ffn_init` — so the mount has to be reissued, or added to the initramfs
`ffn-nfsroot` flow, which is the script that strands the DP when it is wrong.
Reissue it with:

```sh
mount -t nfs -o ro,nolock,vers=3 127.1.2.1:/tmp/dpfs /proc/1/root/opt/dpfs
```

### Do not `wc -c` across this chain

Sizes come from `ls -l`. Streaming `bcm.user` — 196 MB — back through two nested
NFS hops and the PCIe mailbox blew a 300 s marker timeout on the first attempt.
The mount was fine; the check was not.
