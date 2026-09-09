# Debian boot and management on the PA-5220

## Hardware results, 2026-09-09

Both processors have booted Debian MIPS64 BE n64 with
`/usr/lib/systemd/systemd` as PID 1 on the actual PA-5220. Both reported
`systemctl is-system-running` = `running`, zero failed units, and a clean
`dpkg --audit`. Python/SQLite and real SSH sessions also passed on both.

The CP runs kernel `6.18.49-ffn-debian-cp-dirty`; the DP runs
`6.18.49-ffn-debian-dp+`. Both have cgroups and namespaces enabled. Their
filesystems are:

* CP: `127.1.1.1:/opt/ffn-cproot-debian-20260909`, served by the MP.
* DP: `127.1.2.1:/opt/dproot-debian-20260909`, re-exported by the CP, fsid 8.

The successful image contains 94 packages. The first boot exposed three
missing packages in the 91-package staging root: `mount`, `libkmod2`, and
`udev`. All three are now installed in both deployed roots and the VM's
`cproot`. The image preparer refuses a root without mount/udev. Their absence
caused failed filesystem units and an unresolved serial device; installing
them fixed those failures rather than masking the units.

The saved `ffn-octeon.service` startup was then exercised end-to-end on the
PA-5220 and completed successfully. Restarting the CP's `ffn-dpnet.service`
also recovered the DP link; the full checks passed again afterwards. See
`BOOT-VALIDATION-20260909.txt`. Both processors have distinct machine IDs;
the cloned build-root identity is no longer shared between them.

## Direct MP → CP management

Run on the MP as root:

```sh
ffn-cp
ffn-cp 'systemctl is-system-running; systemctl --failed'
ffn-cp 'journalctl -b -u ffn-sshd --no-pager'
```

This is direct SSH over PCIe to `127.1.1.2:22`, with no jump host. The MP
keeps its existing private key; the CP holds the corresponding authorized
public key. `/etc/ffn-ngfw/ssh-cp.conf` pins the CP's host key using
`/etc/ffn-ngfw/cp_known_hosts` and `StrictHostKeyChecking=yes`.
`install-mp-cp-access.sh` reproduces that setup using the host public key
from the deployed root on the MP. No private key is copied into the repo.

DP management from the MP goes through the CP:

```sh
ssh -o UserKnownHostsFile=/etc/ffn-ngfw/plane_boot_known_hosts \
    -o 'ProxyCommand=ssh -F /etc/ffn-ngfw/ssh-cp.conf -W %h:%p ffn-cp' \
    root@127.1.2.2
```

## Saved startup sequence

The MP's existing enabled `ffn-octeon.service` retains its console/NFS/network
ordering. `/etc/systemd/system/ffn-octeon.service.d/90-debian-planes.conf`
selects `/usr/local/sbin/ffn-boot-debian-planes` with a 1200-second startup
timeout. The original unit file is preserved.

1. Check the CP kernel checksum. Stop the MP's BAR-writing pcnet daemon,
   serialize vendor boot tools, reset/stage the CP, finish the console-watch
   period, then restore the MP transport. The known-good boot script is used
   through a private copy with only its kernel path changed.
2. The CP initramfs supervises pcnet from RAM, mounts Debian, validates
   systemd/SSH, retains the old initramfs at `/oldroot`, and execs the static
   PID 1 handoff. Debian starts `ffn-sshd` and `ffn-cp-nfs`.
3. The MP verifies direct SSH, synchronizes the CP's clock from the MP, and
   starts `ffn-dp-boot.service` on the CP.
4. That service stops DP transport writers, prepares the vendor-data mount,
   boots the DP, waits for the recovery agent, then starts `ffn-dpnet.service`
   with its executable copied to `/run/ffn-dp` (tmpfs).
5. The DP mounts its Debian export and hands PID 1 to systemd. Its recovery
   agent and DP-side transport retain their initramfs root. The MP verifies
   the DP's systemd executable and SSH service, and synchronizes its clock.

Only the boot service starts the DP transport. Enabling `ffn-dpnet.service`
independently would let it race vendor operations during a reset.

The CP keeps OpenWrt management tools and NFS-server userspace in the separate
`/opt/ffn-compat` chroot. This preserves their musl runtime; Debian remains
the boot root. `ffn-cp-nfs.service` supplies an explicit PATH including `/bin`
and `/sbin`, because the first systemd invocation otherwise could not find
OpenWrt's grep/insmod. Broadcom/FFN application services are not implied by
a successful Debian OS boot and still need separate integration and tests.

## Build and staged artifacts

### BCM and FE100 integration, 9 September 2026

The active CP kernel is now
`/var/lib/ffn-ngfw/octeon/ffn-vmlinux-systemd-cp-mdio-20260909`, SHA256
`7a95ae5aafdaecea4580c6acc1686e66b6d5e2d4d68c44169ffbd97dc3c3476b`.
`95-mdio-kernel.conf` selects it through `FFN_CP_KERNEL`. The old kernel
listed below remains available for recovery. The kernel fixes Cavium
Clause 45 device addressing; the private guarded boot script also reserves
BCM's DMA pool with `ffn_reserve=0x30000000,64M`.

CP startup now includes `ffn-bcmd.service`, `ffn-front-ports.service`,
`ffn-copper.service`, and `ffn-fe100-links.service`, in that order after
the DP boot prerequisite. BCM startup disables THP, checks the actual DMA
reservation, loads matching modules and owns a single vendor SDK session.
All 24 front data ports are enabled. The tested cabled pairs are:

| Faceplate pair | BCM ports | Measured rate |
|---|---|---|
| 1 / 3 | 28 / 14 | 10G |
| 5 / 13 | 16 / 7 | 10G |
| 23 / 24 | 34 / 35 | 40G CR4 |

Copper firmware is loaded into RAM and MDIO writes are then disabled.
FE100's standalone gearbox initializer precedes its NIF and TMI routines.
BCM ports 3 and 20 subsequently report 100G Ethernet and 12-lane Interlaken
link-up. See [FE100 evidence and limits](../../fe100/LINK-BRINGUP-20260909.md).
This establishes hardware links; sustained packet forwarding and offload
are separate and still unverified. BCM port 2 is a PHY-loopback health
port, not evidence of a DP Ethernet connection.

### Earlier OS-only kernel artifacts

`build-plane-kernels.sh` builds isolated hardware kernels. Then
`build-systemd-boot.sh` embeds private CP/DP initramfs trees with the new
handoff. It needs the two files from `octeon/initramfs` beside it on the VM:
`ffn_init.c` and `ffn-nfsroot-dp.sh`. The DP init uses SDK GCC 4.7.0 with `-G0`
because a freestanding entry point has no CRT to initialize the global pointer.

| Artifact | Staged path | SHA256 |
|---|---|---|
| CP boot kernel, MP | `/var/lib/ffn-ngfw/octeon/ffn-vmlinux-systemd-cp-20260909` | `dcad735153a6d3c6b1f5856f7c8d69abe16f942c90f964cdb0271344c2d98af1` |
| DP boot kernel, compatibility root on CP | `/opt/ffn/ffn-vmlinux-systemd-dp-20260909` | `844eed5ebec2e629963c35e02469743ee4c66ad12e9ee3436e49c1de932d639a` |

The initial 539-MiB root tar predates the three package fixes and service
integration. The deployed roots and scripts, not that old tar alone, describe
the working boot. The native GCC developer-image `-lssp` issue is separate
and remains recorded in `PLANE-DEVELOPMENT.md`.

## Recovery and limits of testing

The MP console broker and preserved kernels provide recovery. The original
`/root/boot618-pcie.sh` still selects the old IRQ-fix kernel, and
`/root/nfsroot_boot.sh` provides its guarded OpenWrt CP recovery sequence.
Keep both files and all known-good staged kernels. Restoring the original
service selection also requires removing the Debian service drop-in and
reloading systemd; do not run two boot orchestrators concurrently.

Processor resets and boot sequences are tested on the PA-5220. A physical
power removal/cold-start test and full firewall forwarding validation are
separate from these OS and management checks.
