# CP control plane on upstream Linux 6.18 (PA-5220, OCTEON III CN73XX)

Everything needed to bring the PA-5220's control-plane OCTEON up on an upstream
kernel instead of the vendor's 4.9 SDK tree, plus the scripts that do it. Captured
from the live appliance before a reinstall, so it survives a wipe.

**Verified on hardware 2026-09-02** — not build-time only:

| | result |
|---|---|
| kernel | upstream `6.18.49` on CN73XX |
| cores | `smp: Brought up 1 node, 8 CPUs`, per-core CIU3 `octeon_wdt` IRQs |
| memory | `MemTotal: 8137044 kB` — full 8 GiB, **no `mem=` passed** |
| PCIe | 3 root complexes trained; PEM0 correctly skipped; BCM88375 FE100 enumerated at `0001:01:00.0/.1` |
| transport | MP↔CP virtual ethernet, 300/300 pings at 1400 B, 0% loss, RTT mdev 0.052 ms |
| userland | NFS root from the MP's SSD; bash 5.2.37 + Python 3.12.12 execute from it; writes persist |
| shell | `ssh root@127.1.1.2` over PCIe |

## Bring-up order (it matters)

1. `systemd/ffn-octconsoled.service` — the console broker, **as its own unit**.
   This was the original boot blocker: `ffn_octconsoled.py start` does not
   daemonize, so calling it from the oneshot made the oneshot hang and the CP
   never booted at boot time.
2. `systemd/ffn-octeon.service` — the oneshot that boots the OCTEON. Note it
   uses `Wants=` on the broker, **not `Requires=`**. `Requires=` propagates the
   broker's `Restart=always` into a oneshot that resets the OCTEON, which
   re-reset the CP under a live session.
3. `boot618-pcie.sh` — reset, load u-boot, stage the kernel, boot.
4. `tools/pcnet-up.sh` (in `/opt/ffn-ngfw-v2`) — brings the host end of the
   transport up and reprograms BAR1 index 1, which the reset clears.
5. `setup_cp_sshd.sh` — configures sshd inside the CP's NFS root, from the MP.
6. `nfsroot_boot.sh` — orchestrates 3+4 in the right order, polling for the
   boot's own done marker instead of sleeping a guess.

## HAZARD: never reset the OCTEON with the host transport running

This downed the MP and needed a hard power cycle. From `journalctl -b -1`:

    ffn_pcnetd: tx=327 rx=327 txdrop=0 rxdrop=0          <- live and busy
    pcieport 0000:00:01.0: PCIe Bus Error: Uncorrectable (Non-Fatal), [14] CmpltTO
    pci 0001:01:00.0/.1/.2: AER: can't recover (no error_detected callback)
    pcieport 0000:00:01.0: AER: device recovery failed
    ... repeating every ~12s until the box died

The host daemon drives OCTEON DRAM through the BAR window continuously. Reset the
OCTEON underneath it and every access takes a Completion Timeout; because nothing
claims the endpoint, AER recovery cannot act and just retries forever.

`boot618-pcie.sh` now stops `ffn-pcnetd` first and **aborts** rather than
resetting under a live BAR writer. Do not remove that guard.

**Do not infer safety from the last reset working.** Six resets the same day were
harmless because the transport was idle or had no peer; the one that killed the
box followed a 300-packet load test.

Two things that look like the cause and are not: the lock (both paths flock
`/run/ffn-octeon-ctl.lock` — verified), and an external `timeout` killing
`pcnet-up.sh` (the AER errors began before it would have run — though wrapping
these in an external `timeout` is still wrong, since `pcnet-up.sh` says "never
kill it" about its `oct-remote-csr` child).

## Kernel patches

`../0001-ffn-octeon-6.18.patch` is tranche 1 (boots, no networking).
`tranche2-pcie/` is tranche 2 — the OCTEON III PCIe port and the fixes found
booting it:

* `0002-ffn-authored-edits.patch` — FFN's own edits to upstream files:
  `setup.c` (ffn_reserve, `__fdt` by name, `octeon_dma_bar_type`, the
  `ext_core_mask` recovery), `octeon-feature.h`, `cvmx-helper-jtag.c/.h`, both
  Makefiles.
* `ffn-cvmx-compat.h` — the shim. **`cvmx.h` and `cvmx-helper*.h` must NOT be
  replaced with SDK copies**; they carry inline functions and types shared with
  upstream's already-compiled objects. Shim instead.
* `topology.h` — the `pcibus_to_node` self-define, without which asm-generic's
  macro expands inside the SDK's function definition and the error is reported
  several headers away from the cause.
* `cvmx-csr-enums.h` — **two UART enums only**, not the whole SDK file, which
  also redefines types upstream already has (15 redeclaration errors).
* `0003-gmxx-prototype.hunk` — one prototype, restored because replacing
  `cvmx-gmxx-defs.h` broke upstream's *own* helper files.
* `0004-qlm-gating-lines.txt` — where `cvmx-qlm.c` is gated behind
  `FFN_QLM_HAVE_BGX` / `FFN_QLM_OWN_CDR_ERRATA`.
* `IMPORTS.md` — the SDK files imported verbatim, with line counts and md5s.
  **The vendor sources themselves are deliberately NOT in this repo** pending a
  publish-policy decision; the manifest makes the import reproducible from the
  SDK.
* `kernel-config-deltas.txt` — the config symbols that matter. `CONFIG_TUN=y`
  is required for the transport and was the actual blocker, not
  `CONFIG_STRICT_DEVMEM` (which is absent on MIPS).

## Three traps worth knowing before touching this

1. **Replacing an upstream file silently drops integration points.** Three
   instances: the `gmxx` prototype above; a duplicate CDR errata definition
   (upstream keeps it in `cvmx-helper-errata.c`, the SDK in `cvmx-qlm.c`); and
   `octeon_dma_bar_type`, which upstream's inlined `pcie-octeon.c` was the only
   thing setting — leaving `octeon_pci_dma_init()` to hit `default: BUG()`.
   `*-defs.h` are safe to replace for register content only.
2. **An undefined macro reports errors that name other things.** `CVMX_SHARED`
   (empty in the SDK, absent upstream) made one declaration unparseable and
   produced five errors naming three unrelated symbols.
3. **`CONFIG_DEVTMPFS_MOUNT=y` does nothing on an initramfs boot.** The kernel
   runs `/init` directly and never calls `prepare_namespace()`, so it never
   mounts `/dev`. With no `/dev/console` node, `/init` gets closed stdio and a
   perfectly good boot prints *nothing*. `/init` must mount devtmpfs and re-exec
   its own stdio before printing.

## Userland

The CP's userland is an NFS root served from the MP's SSD, because the initramfs
is RAM and loses everything on reboot. `setup_cp_sshd.sh` documents the rest,
including why the vendor `/opt/dpfs` tree cannot be used: its glibc is 2.16 and
SIGSEGVs the moment bash starts on a 6.18 kernel.
