# Tranche 3: the BCM kernel modules on the 6.18 CP

Both FFN BCM modules now build and load on the 6.18 control plane, and the
BCM88375 answers. Previously they existed only for 4.9.

    ffn_bcm 0001:01:00.0: BCM88375 CMIC bound: bar2 8388608 bytes,
                          ident 0x00008375, /dev/ffn_bcm
    ffn_bde: PAXB big-endian PIO mode on (BAR0+0x2030 reads 1)
    ffn_bde: full PAXB init done (dma_hi_bits 1)
    ffn_bde: DMA region 4 MB: dma_handle 0x2400000 phys 0x2400000
    ffn_bde: ready. 1 device(s).

`ffn_bcmctl` reads live registers through `/dev/ffn_bcm`: LEDUP0 CTRL
(`bar2+0x20000`) = 0, CLK_DIV (`bar2+0x20050`) = 0x005b8d80 = 6,000,000.

## Build

    make -C <6.18-tree> M=octeon/kctl ARCH=mips \
         CROSS_COMPILE=.../gcc-14.4.0-nolibc/mips64-linux/bin/mips64-linux- modules

Drop the Makefile's `HOSTCFLAGS="-fcommon"` — that is a 4.9 `scripts/dtc`
workaround. Run BOTH `make modules_prepare` and `make modules` in the kernel
tree first: the second generates the real `Module.symvers` (7920 symbols with
this config). Creating an EMPTY Module.symvers to satisfy the Makefile makes
modpost declare every core symbol undefined.

`ffn_bcmctl` must use the SDK's gcc (`.../sdk51/OCTEON-SDK/tools/bin/
mips64-octeon-linux-gnu-`). It is freestanding, and gcc 14 emits `strlen`/
`memset` calls the nolibc toolchain cannot resolve. Freestanding means the SDK
compiler's age does not matter here.

## 0005-pcie-octeon-map-irq-not-__init.patch — a bug in tranche 2

Loading ANY PCI driver oopsed the CP:

    Unable to handle kernel paging request at virtual address 0000000000000001
    ra : pci_assign_irq+0x90/0xf0

OCTEON dispatches `pcibios_map_irq` through the long-lived function pointer
`octeon_pcibios_map_irq` (`pci-octeon.c:74-80`), which `pcie-octeon.c` sets to
one of two implementations. Tranche 2 had marked both `__init`, so `.init.text`
was freed after boot and the pointer dangled. It survives boot enumeration —
nothing binds a driver then — and dies on the first post-boot `pci_enable_device`.
Upstream 6.18.49 does not mark them `__init`; this was ours. `pci_assign_irq`
now assigns IRQ 13 to the BCM.

## Source fixes in octeon/kctl

- `strlcpy` -> `strscpy` in both files (removed in 6.8).
- `.llseek = no_llseek` dropped in `ffn_bcm.c` (removed in 6.12). Exactly
  equivalent: `fs/open.c:976` clears FMODE_LSEEK when `.llseek` is NULL and
  `vfs_llseek` returns -ESPIPE. `noop_llseek` would be WRONG — it accepts seeks.
- In `ffn_bde.c` the strlcpy return WAS a truncation test (`n >= sizeof(cmd)`,
  then `cmd[n-1]`). strscpy returns `-E2BIG` instead, so the check is now
  explicit (`r < 0`). Assigning straight to the existing `size_t n` happens to
  still reject truncation via unsigned wraparound, but that is not something to
  leave in a driver, and would index before the buffer if `n` were ever signed.

## DO NOT enable CONFIG_PCI_MSI on this chip

`ffn_bde` reports `pci_enable_msi failed; no interrupt handler. Command 9 will
block forever (thread parks).` The obvious fix is not available:

    Kernel panic - not syncing: request_irq(OCTEON_IRQ_PCI_MSI0) failed

`CONFIG_PCI_MSI=y` pulls in `arch/mips/pci/msi-octeon.c`, which is CIU-era
(OCTEON I/II) code. It requests `OCTEON_IRQ_PCI_MSI0`, an interrupt number that
does not exist on the CN73XX's CIU3, and it PANICS rather than degrading. This
platform is OCTEON III only, so that file is not a usable path — it needs CIU3
support, or the BDE needs a legacy-INTx fallback.

INTx is the more promising route: the fixed `pci_assign_irq` already assigns the
BCM a real IRQ (13), so `request_irq(pdev->irq, ..., IRQF_SHARED)` should work.
Note the BDE's PAXB init currently DISABLES the INTx path in favour of MSI —
`CMICD_TO_PCIE_INTR_EN 0x00000001 -> 0x00000000 (MSI)` — so an INTx fallback
must leave that register at 1.

Also: rebuild the modules after ANY kernel config change. `CONFIG_PCI_MSI=y`
grows `struct pci_dev`, and vermagic does not encode the config, so a stale
`.ko` loads happily against wrong offsets. Module sizes are a quick tell:
478360/547720 without MSI, 478640/553848 with.

And when restoring a saved `.config`, do NOT use `cp -a` — it preserves the
backup's mtime, so `.config` looks older than `include/generated/autoconf.h`
and kbuild silently reuses the stale one. Use `cat > .config; touch .config;
make syncconfig`, then verify with
`grep -c 'CONFIG_PCI_MSI 1' include/generated/autoconf.h`.

## State and what remains

`ffn_bcm.ko` is a register door: single reg rd/wr, one S-channel transaction,
LED load, INFO ioctl. It does no DNX init, no PMD firmware and no netdevs, so it
CANNOT bring a port up. `ffn_bde.ko` is the path — the vendor `bcm.user` diag
shell (196 MB, statically linked, so no glibc-2.16 loader problem) attaches
through it and cint scripts do the work. Launch with `tools/ffn-bcm-sdk.sh
<cmdfile>`, which is idempotent and refuses to insmod onto a PCIe path that has
already seen a Data Bus Error. Note its warning: a config COPY is only read when
`BCM_CONFIG_FILE` names it, because bcm.user carries absolute
`/usr/share/broadcom` paths — every earlier config change was silently inert.

Not yet done: running bcm.user to DNX init on 6.18, and therefore no ports up.

## Coexistence hazard

The chip RETAINS its PAXB byte-order across a CP reboot. Loading `ffn_bcm`
first on a chip the BDE previously switched to big-endian PIO mode makes it
report `ident 0x75830000` instead of `0x00008375` — byte-swapped. Nothing is
broken, but `ffn_bcm`'s register reads are not trustworthy in that state.
