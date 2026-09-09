# Debian on the PA-5220 processors — 2026-09-09

**Later hardware boot update:** both planes now boot Debian with systemd as
PID 1, and the MP has direct, pinned-key CP management through `ffn-cp`.
See [BOOT-AND-MANAGEMENT.md](BOOT-AND-MANAGEMENT.md) for the working setup.
The sections below describe the earlier chroot milestone and its then-open gaps.

Both the CN73XX control plane and CN78XX data plane now run the same complete
Debian MIPS64 BE n64 userspace in separate hardware chroots. Both accept real
public-key SSH sessions on port 2222. Their existing OpenWrt boot roots and
recovery transports remain active. This is not yet a Debian PID 1 boot.

## Verified on both processors

`HARDWARE-VALIDATION-20260909.txt` records the MP's
`check-plane-candidates.sh` run. It connects to the actual MIPS processors,
not QEMU. Tests cover SSH authentication/session execution, a present
`/sbin/init`, clean `dpkg --audit`, iproute2, Python 3.14.7, BE byte order,
64-bit pointers, SQLite queries, threads, decimal arithmetic, and BusyBox
`printf` with `%s` and `%f`. All passed on both processors. This resolves
the earlier uncertainty about those BusyBox formatting cases on hardware.

The staging root contains all 91 packages from the VM's current `cproot`,
plus upstream CPython/OpenSSH files outside dpkg ownership. The old deployed
Debian tree had only 65 registered packages and lacked init/iproute2; it is
preserved separately. Do not confuse the two trees.

OpenSSH requires `sshd-session` and `sshd-auth` in addition to `sshd`.
`sshd -t` alone did not detect their absence in the old staging procedure.
The builder now retains both helpers; the candidate explicitly configures
their paths, and an actual login validates them. Host keys are generated
on the MP separately for each processor; existing authorized public keys
are retained. SSH is bound to each plane's transport address.

## Paths and access

| Item | Location |
|---|---|
| VM reusable root tar | `/mnt/clones/debian-mips64/images/plane-20260909-r2.tar` |
| MP CP candidate root | `/opt/ffn-cproot-debian-20260909` |
| MP DP candidate root | `/opt/ffn-cproot-owrt/opt/dproot-debian-20260909` |
| CP view of DP export | `/opt/dproot-debian-20260909` (NFS fsid 8) |
| Mount on each processor | `/mnt/debian-candidate` |
| Hardware test log on MP | `/var/tmp/ffn-debian-plane-validation.log` |

From the MP, with the host keys pinned in the specified file:

```sh
ssh -o UserKnownHostsFile=/var/tmp/ffn-debian-candidate-known-hosts -p 2222 root@127.1.1.2
ssh -o UserKnownHostsFile=/var/tmp/ffn-debian-candidate-known-hosts -J root@127.1.1.2 -p 2222 root@127.1.2.2
```

These test daemons are not boot-persistent. The candidate root includes an
`ffn-sshd.service` for eventual systemd startup. Candidate APT sources disable
the VM-only loopback mirror; configure a reachable MP archive before installing
more packages from these hardware sessions.

Tar SHA256: `4092e83e4e2aecaed9dca5bec1a777329fe933a75c1f35afee4a91e67dd4bbcf`.
Matching kernel modules were added to the deployed roots after extraction.

## Kernel candidates built and staged, not boot-tested

`build-plane-kernels.sh` copies each existing kernel tree to an independent
directory and retains its plane-specific hardware patches and initramfs.
Both builds passed and produced big-endian ELF64 MIPS kernels and matching
modules. Saved configurations are in `kernel-config-{cp,dp}-20260909`.

New options include cgroups, pids/memory/CPU controllers, namespaces, service
isolation, and tmpfs ACL/xattr support. CP nfsd is built in, avoiding reliance
on the old kernel's `nfsd.ko` when it serves the DP root.

| Plane | Staged kernel | SHA256 |
|---|---|---|
| CP, on MP | `/var/lib/ffn-ngfw/octeon/ffn-vmlinux-debian-cp-20260909` | `0b8d9b2146edb4d057eac982efa36b85798d9dbe50bcb114bc9dc48b91d146a6` |
| DP, on CP | `/opt/ffn/ffn-vmlinux-debian-dp-20260909` | `1f6d5ab654df0ffd6491fe7ee916385959072e5f888933d8146c1eeae937be85` |

Matching modules are installed in each candidate Debian root. Original boot
scripts still select the known-good kernels. The new kernels keep the old
initramfs boot policy; booting one alone does not select Debian.

## Remaining boot integration

1. Validate the new kernels on hardware, first with the proven root policy.
2. Add a selectable Debian root and a PID 1 exec of systemd. The current CP
   init starts a chrooted shell; the DP handoff changes PID 1's root but does
   not yet launch Debian systemd. Preserve an initramfs recovery agent and
   transport on both planes, with a fallback before committing the switch.
3. Keep pcnet/dpnet executables and their complete runtime outside NFS: they
   must not fault their own code in over the link they service. Integrate
   their supervision, the CP's NFS server/re-export, and FFN application
   services into the new startup. The candidate root does not yet carry
   those production services.
4. Test cold boot, transport recovery, CP-to-DP NFS availability and rollback.

`nfs-common` is not the sole remaining blocker. The initramfs already mounts
the root before Debian runs. Debian NFS tools remain useful for later mount
management; cgroups, PID 1 handoff and transport/NFS-server service lifetimes
are the boot-critical issues.

The older developer-image verification also still fails on GCC's missing
`-lssp`. The working build chroot has Claude's compatibility workaround, but
the archived developer image has not been repaired or revalidated by this
plane-development work.

No processor was reset during this work. Follow the existing controlled boot
sequence: resetting the CP with the MP's BAR-writing pcnet daemon active can
wedge the MP. Preserve the staged known-good kernels and the DP recovery agent.
