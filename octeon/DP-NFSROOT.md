# Booting the DP over NFS: what works, what cannot, and what is left

The DP currently **chroots** into an NFS mount (`dproot/ffn-dp-nfsroot.sh`).
Making it genuinely **root** over NFS means the initramfs mounts the CP's
export and re-roots into it. This records the three constraints that shape
that, the one blocker, and the state everything is in.

Layering is unchanged, from [`NFS-LAYERING.md`](NFS-LAYERING.md):

    MP 127.1.1.1  --pcnet-->  CP 127.1.1.2 / 127.1.2.1  --dpnet-->  DP 127.1.2.2
    /opt/ffn-cproot-owrt/opt/dproot   =   /opt/dproot on the CP   =   / on the DP

## 1. `root=/dev/nfs nfsroot=... ip=...` CANNOT WORK

`NFS-LAYERING.md` proposes putting this on the kernel command line. It cannot
work, and the reason is structural rather than a configuration gap.

The CP↔DP link is a **TAP device created by a userspace daemon** —
`ffn_dpnetd.c` does `open("/dev/net/tun")`, `TUNSETIFF`, `IFF_TAP`. The
kernel's `ip=` autoconfiguration and its Root-NFS mount both run in
`prepare_namespace()`, *before* any userspace exists. At that moment there is
no interface to configure and no route to the server.

`CONFIG_ROOT_NFS=y`, `CONFIG_IP_PNP=y`, `CONFIG_TUN=y` and `CONFIG_NFS_FS=y`
are all set in `fwdport/linux-6.18.49-dp` — they are simply unreachable for
this transport. The DP's real NIC is the 40G BGX link to the switch, which is
not a path to the MP.

**So the mount must happen from userspace, from the initramfs.**

## 2. `pivot_root` CANNOT BE USED EITHER

The DP's `/` is rootfs — `/proc/mounts` reads `rootfs / rootfs rw,size=…`.
rootfs's mount has no parent, which is the documented `EINVAL` case for
`pivot_root(2)`. `switch_root` exists for exactly this, and its semantics are
`MS_MOVE` of the new root onto `/` followed by `chroot`.

busybox `switch_root` in turn refuses unless it is PID 1, and `ffn_init`'s
`run_shell()` **forks** before exec. So the work splits:

| where | what | can it fail? |
|---|---|---|
| `/sbin/ffn-nfsroot` | transport, mount, validate, stage | yes — recoverable |
| `ffn_init`, PID 1 | `MS_MOVE` + `chroot` | no |

with a flag file as the handover. `switch_root_to()` in `initramfs/ffn_init.c`
implements the PID 1 half; its syscall numbers were read out of
`arch/mips/include/generated/uapi/asm/unistd_n64.h` rather than remembered
(`chdir` 5078, `chroot` 5156, `mount` 5160, `MS_MOVE` 8192) and verified in the
compiled binary — chdir appears twice, chroot once, mount four times (the three
pseudo-filesystems plus the move).

## 3. THE ORDERING IS FORCED: agent → dpnet → mount → switch

This is the constraint that is easiest to miss and most expensive to discover.

The CP's `ffn_dpnetd` **refuses to start** until it can read the DP agent's
magic at BAR offset 0x400000:

    ffn_dpnetd: BAR1 window canary failed: read 0x0000000000000000 at BAR
    offset 0x400000, expected the ffn-dpsh magic 0x46464e4450534832.

That magic is written by `ffn_dpagent2` on the DP. So:

* the agent must be up before the CP's dpnet end can start,
* the CP's dpnet end must be up before the DP can mount anything,
* therefore **the agent must be running before the nfsroot flow, not after**.

An nfsroot flow that runs first and waits for the CP deadlocks against a CP
daemon that is waiting for the agent.

## 4. THE BLOCKER: the DP init's source does not exist

The DP's deployed `init` is **13313 bytes** and starts `/sbin/ffn_dpagent2`.
Every `ffn_init.c` on disk is the same **9491-byte** version with **zero**
`dpagent` references:

    octeon/initramfs/ffn_init.c                                    (repo)
    /mnt/clones/ffn-build/rootfs/opt/ffn-ngfw-v2/octeon/initramfs/
    /mnt/clones/ffn-build/recovery/opt/ffn-ngfw-v2/octeon/initramfs/
    /mnt/clones/ffn-image-build/initramfs/

The working binary is the only artifact. Building from any available source
yields an init with no agent, and a DP that boots cleanly with 40 cores and is
**completely unreachable**. That was verified the hard way: recovery was
`FFN_DP_KERNEL=/opt/ffn/ffn-vmlinux-6.18.49-dp-pknd4 sh /opt/ffn/dpboot8.sh`,
which brought it straight back.

[`ffn-initramfs-source-gap.md`] already recorded this class of problem —
"rebuilding as-is costs CP access". It cost DP access here.

The agent branch is reconstructible; its strings give the shape, and it sits
after the nfsroot branch in the deployed binary (offsets 9848 → 9992):

    FFN> DP session agent: /sbin/ffn_dpagent2 (mailbox at phys 0x400000; use
         ffn-dpsh on the CP)
    FFN> ffn_dpagent2 returned; restarting it

Note it must become **non-blocking** rather than a straight `for(;;)` copy of
the nfsroot branch, because of constraint 3.

## 5. Why /sbin/ffn-nfsroot is DISARMED in the initramfs

In the deployed init the nfsroot branch precedes the agent branch and loops
forever. So **merely having `/sbin/ffn-nfsroot` present strands the DP** — the
nfsroot flow loops and `ffn_dpagent2` never starts.

It is therefore parked as `sbin/ffn-nfsroot.DISARMED` in
`/mnt/clones/initramfs`, with a README beside it. Re-arming needs BOTH a fixed
init (section 4) and the ordering from section 3. Renaming it back on its own
strands the DP.

`sbin/ffn_dpnetd` was left in place: nothing auto-runs it, so it is inert, and
having it there is a genuine improvement — today `ffn-dpnet-up6.sh` stages the
daemon into DP DRAM and reads it back out through `/dev/mem` on every boot
purely because the DP has no local copy.

## What is built and verified

* `initramfs/ffn-nfsroot-dp.sh` — starts `ffn_dpnetd --role dp`, waits for the
  CP, mounts `127.1.2.1:/opt/dproot` with `nolock,vers=3`, proves the mount by
  checking for `/bin/busybox`, binds `/proc`, `/sys`, `/dev` and the old root
  into it, and writes the handover flag. Plane-guarded on core count (8 = CP,
  40 = DP) because both kernels share one `CONFIG_INITRAMFS_SOURCE`.
  Fail-safe: exits non-zero so `ffn_init` falls through to the boot flow that
  works.
* `initramfs/ffn_init.c` — `switch_root_to()`, plus a prominent warning that
  this file is not the deployed source.
* `/opt/dproot` populated with the script, `ffn_dpnetd`, and `newroot` /
  `oldroot` mountpoints.
* The CP's NFS server verified serving: 8 `[nfsd]` threads, listening on 2049,
  `fsid=7` on the re-export, and the CP successfully mounted its own export.
  (`pidof rpc.nfsd` finding nothing is a false alarm — it spawns kernel
  threads and exits.)
* Build/stage pipeline proven: cpio regen preserving the six character devices,
  kernel rebuild, strip, and stage, md5-verified at every hop.

## Current state

DP is on `ffn-vmlinux-6.18.49-dp-pknd4`: 40 cores, `6.18.49`, agent v2 up,
CP↔DP link 0% loss. `ffn-vmlinux-6.18.49-dp-nfsroot` is staged but **must not
be booted** until section 4 is resolved.

## Two operational traps found on the way

**`nohup` is not in the CP's busybox.** `nohup sh dpboot8.sh` printed
"nohup: not found" and the boot never ran, while the wrapper reported success
because it only checked that a pid existed. Use `setsid`, and verify the child
actually started rather than trusting `$!`.

**`pkill -f <pattern>` matches its own command line.** A `pkill -f
"http.server"` in a command that also mentions `http.server` kills the calling
shell — exit 255, twice. Kill by pid, or keep the pattern out of the caller's
argv by putting it in a script file.
