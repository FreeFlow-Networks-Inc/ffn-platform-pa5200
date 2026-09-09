# The DP roots over NFS

**Working.** The DP's `/` is the CP's `/opt/dproot` export, mounted at boot by
the initramfs and switched into by pid 1. Verified on the live PA-5220:

    127.1.2.1:/opt/dproot / nfs rw,relatime,vers=3,...,nolock,proto=tcp

    /proc/1/root            bin dev etc lib lib64 mnt overlay proc rom root
                            sbin sys tmp usr var www
    .../etc/openwrt_release DISTRIB_ID='OpenWrt'  DISTRIB_RELEASE='24.10.4'
    chroot /proc/1/root /bin/busybox uname -srm
                            Linux 6.18.49-00007-g616977ba1b11 mips64
    /usr/bin                65 binaries
    /bin/sh                 runs

Layering unchanged, from [`NFS-LAYERING.md`](NFS-LAYERING.md) — the DP's
filesystem is authored on the MP's SSD and needs no staging:

    MP 127.1.1.1  --pcnet-->  CP 127.1.1.2 / 127.1.2.1  --dpnet-->  DP 127.1.2.2
    /opt/ffn-cproot-owrt/opt/dproot  =  /opt/dproot on the CP  =  / on the DP

Kernel: `ffn-vmlinux-6.18.49-dp-nfsroot`. Boot it with
`FFN_DP_KERNEL=/opt/ffn/ffn-vmlinux-6.18.49-dp-nfsroot sh /opt/ffn/dpboot8.sh`,
but see **Bring-up order** — the CP end of dpnet has to be started at the right
moment, so use the orchestration rather than dpboot8 alone.

## Three constraints that shape the design

### `root=/dev/nfs nfsroot=... ip=...` cannot work

`NFS-LAYERING.md` proposed this. It cannot work, structurally. The CP↔DP link
is a **TAP device created by a userspace daemon** — `ffn_dpnetd.c` does
`open("/dev/net/tun")`, `TUNSETIFF`, `IFF_TAP`. The kernel's `ip=`
autoconfiguration and Root-NFS mount both run in `prepare_namespace()`, before
any userspace exists, so there is no interface to configure and no route to the
server at that point. `CONFIG_ROOT_NFS=y`, `CONFIG_IP_PNP=y`, `CONFIG_TUN=y`
and `CONFIG_NFS_FS=y` are all set and simply unreachable for this transport.
The DP's real NIC is the 40G BGX link to the switch, which is not a path to the
MP.

**The mount has to come from userspace, i.e. from the initramfs.**

### `pivot_root` cannot be used

The DP's `/` is rootfs (`/proc/mounts`: `rootfs / rootfs rw,size=...`). rootfs's
mount has no parent, which is the documented `EINVAL` case for `pivot_root(2)`.
`switch_root` exists for this, and its semantics are `MS_MOVE` of the new root
onto `/` then `chroot`.

busybox `switch_root` refuses unless it is pid 1, and `ffn_init`'s
`run_shell()` **forks** before exec. So the work splits:

| where | what | can fail? |
|---|---|---|
| `/sbin/ffn-nfsroot` | transport, mount, validate, stage | yes — recoverable |
| `ffn_init`, pid 1 | `MS_MOVE` + `chroot` | no |

The handover is `/ffn-switch-root`, written by the script and found by pid 1.
It is self-clearing: it lives in the old root, so after a successful switch it
is no longer at `/` and cannot re-trigger.

`switch_root_to()`'s syscall numbers were read from
`arch/mips/include/generated/uapi/asm/unistd_n64.h`, not remembered (`chdir`
5078, `chroot` 5156, `mount` 5160, `MS_MOVE` 8192), and checked in the compiled
binary: chdir twice, chroot once, mount four times (three pseudo-filesystems
plus the move).

### Bring-up order is forced: agent → dpnet → mount → switch

The CP's `ffn_dpnetd` **refuses to start** until it can read the DP agent's
magic at BAR offset 0x400000:

    ffn_dpnetd: BAR1 window canary failed: read 0x0000000000000000 at BAR
    offset 0x400000, expected the ffn-dpsh magic 0x46464e4450534832.

That magic is published by `ffn_dpagent2` on the DP. So the agent must be live
before the CP's dpnet end can start, and dpnet before anything can be mounted.
An nfsroot flow that runs first and waits for the CP **deadlocks** against a CP
daemon waiting for the agent — which is exactly what the first attempt did.

Hence `ffn_init` starts the agent **first and in a child** (`supervise_forever`),
naps 2s so the magic is published, then runs the nfsroot flow once, then
switches. On the CP side the orchestration **polls `ffn-dpsh --status` for
"agent v2"** rather than sleeping a guessed interval — and that call is also
what programs the BAR window in the first place.

## The agent branch was reconstructed, not inherited

The DP's previously deployed `init` was 13313 bytes, built with **Cavium SDK
gcc 4.7.0**, and its source was never committed: every `ffn_init.c` on disk was
the same 9491-byte version with **no agent branch at all**. Building from one of
those and booting it gives a DP that comes up cleanly on 40 cores and is
**completely unreachable**, because the mailbox agent never starts. That
happened; recovery was one command against the previously staged kernel:

    FFN_DP_KERNEL=/opt/ffn/ffn-vmlinux-6.18.49-dp-pknd4 sh /opt/ffn/dpboot8.sh

**Never delete the previously staged DP/CP kernels** — that is the whole
recovery path.

A strings diff against the deployed binary showed the gap was exactly three
strings, now reproduced byte-for-byte so a future diff still matches:

    /sbin/ffn_dpagent2
    FFN> DP session agent: /sbin/ffn_dpagent2 (mailbox at phys 0x400000; use ffn-dpsh on the CP)
    FFN> ffn_dpagent2 returned; restarting it

Two deliberate differences from the deployed shape, both forced by the ordering
above: the agent is supervised in a **child** rather than a `for(;;)` in pid 1,
and it starts **before** the nfsroot branch. In the deployed binary the nfsroot
branch came first (string offsets 9848 then 9992) and looped forever, which is
why merely adding `/sbin/ffn-nfsroot` to the DP initramfs was enough to strand
the DP.

The reconstructed init is built with the **same** SDK gcc 4.7.0, to keep the
compiler out of the variables.

## ffn-dpsh shows the OLD root, and that is deliberate

`readlink /proc/1/root` reports the NFS root, but commands run through
`ffn-dpsh` see the **initramfs**. That is by design, not a bug: the agent is
forked before the switch, and `chroot()` affects only the caller and its future
children. So the control channel stays on the initramfs and is **independent of
the NFS root** — a bad export or a dead link cannot take `ffn-dpsh` down with
it.

Consequences when debugging:

* `cat /etc/openwrt_release` through `ffn-dpsh` looks missing. Read the new root
  as `/proc/1/root/...`, or `chroot /proc/1/root ...` to run its binaries.
* `/proc/mounts` is namespace-wide, so it shows the NFS mount at `/` regardless
  of which root the querying process has. It lists the `rootfs` line too; that
  is normal after `MS_MOVE`, exactly as `switch_root` leaves it.

## The initramfs is kept at /oldroot, on purpose

`MS_MOVE` detaches the old root mount, so the script bind-mounts it in at
`<newroot>/oldroot` first. Verified present after the switch, with the daemon in
it:

    /proc/1/root/oldroot        bin dev etc ffn-switch-root init lib proc sbin sys tmp
    .../oldroot/sbin/ffn_dpnetd 797576 bytes

**The transport's binary must never be fetched over the transport.**
`ffn_dpnetd` provides the link the NFS root is served across; if a page of its
own executable had to be faulted in from that mount, the fault would need the
daemon that is blocked servicing it. Keeping `/oldroot` means it stays
restartable without touching NFS. It is statically linked too, so there is no
shared library to fault either. `ffn_dpnetd` was also added to the initramfs
permanently, which removes a real cost: `ffn-dpnet-up6.sh` otherwise stages the
daemon into DP DRAM and reads it back through `/dev/mem` on every boot purely
because the DP had no local copy.

## Also worth not re-deriving

The CP's NFS server is genuinely serving: 8 `[nfsd]` threads, listening on
2049, `fsid=7` on the re-export (mandatory — it is an NFS re-export and nfsd
refuses one without a filesystem id), and the CP can mount its own export.
**`pidof rpc.nfsd` finding nothing is a false alarm** — it spawns kernel threads
and exits.

`nolock` on every hop, deliberately: the CP's own root uses it, and lockd across
two nested NFS re-exports is not something to inherit by accident.

The script is plane-guarded on core count (8 = CP, 40 = DP) because it ships in
an initramfs tree that could be shared. In practice the CP and DP inits are
separate binaries from separate cpios (`ffn-cp-rootfs.cpio` from
`fwdport/rootfs/tree`, `ffn-dp-rootfs.cpio` from `/mnt/clones/initramfs`), so a
DP rebuild cannot affect the CP — but the guard costs nothing and the trees have
drifted before.

## Two operational traps found on the way

**`nohup` is not in the CP's busybox.** `nohup sh dpboot8.sh` printed
"nohup: not found", the boot never ran, and the wrapper reported success because
it only checked that a pid existed. Use `setsid`, and verify the child actually
started rather than trusting `$!`.

**`pkill -f <pattern>` matches its own command line.** A `pkill -f "http.server"`
in a command that also mentions `http.server` kills the calling shell — exit
255, twice. Kill by pid, or keep the pattern out of the caller's argv by putting
it in a script file.
