# Tranche 2 — OCTEON III PCIe: SDK files imported or replaced

Source: `/mnt/clones/sdk51/OCTEON-SDK/linux/kernel/linux` (OCTEON SDK 5.1, cvmx executive is BSD-3).
Reproduce by copying each file from that path to the same relative path in the 6.18 tree.

## Replaced (upstream had a pruned copy; SDK version is CN7XXX-aware)

| file | upstream lines | SDK lines | SDK md5 |
|---|---|---|---|
| arch/mips/include/asm/octeon/cvmx-ciu-defs.h | 176 | 16894 | 8dcffb97b747 |
| arch/mips/include/asm/octeon/cvmx-dpi-defs.h | 874 | 3527 | 114d3a542960 |
| arch/mips/include/asm/octeon/cvmx-gmxx-defs.h | 2259 | 13472 | 6e7203f2c1b7 |
| arch/mips/include/asm/octeon/cvmx-mio-defs.h | 4396 | 12332 | 675105959be9 |
| arch/mips/include/asm/octeon/cvmx-npei-defs.h | 3925 | 7439 | f9d3f5398285 |
| arch/mips/include/asm/octeon/cvmx-pci-defs.h | 2037 | 4585 | ba25c19f3cd5 |
| arch/mips/include/asm/octeon/cvmx-pciercx-defs.h | 368 | 11223 | 0545b1e63889 |
| arch/mips/include/asm/octeon/cvmx-pemx-defs.h | 651 | 4586 | 7714a4d6772d |
| arch/mips/include/asm/octeon/cvmx-pescx-defs.h | 579 | 1055 | f2de98a79f3d |
| arch/mips/include/asm/octeon/cvmx-pexp-defs.h | 224 | 4982 | 3dcc4a491d90 |
| arch/mips/include/asm/octeon/cvmx-rst-defs.h | 278 | 930 | 458a48022727 |
| arch/mips/include/asm/octeon/cvmx-sli-defs.h | 129 | 13168 | 92f6e13f047f |
| arch/mips/include/asm/octeon/cvmx-sriox-defs.h | 1614 | 4662 | 8bf5377acda1 |
| arch/mips/pci/pcie-octeon.c | 2094 | 889 | f9d7e7f6a525 |

## Added (absent upstream)

| file | lines | SDK md5 |
|---|---|---|
| arch/mips/cavium-octeon/executive/cvmx-pcie.c | 2591 | a1a841b44b43 |
| arch/mips/cavium-octeon/executive/cvmx-qlm-tables.c | 480 | 423e9d942a41 |
| arch/mips/cavium-octeon/executive/cvmx-qlm.c | 3343 | 5fb576ef2f48 |
| arch/mips/include/asm/octeon/cvmx-bgxx-defs.h | 8355 | 3a13dbeaad54 |
| arch/mips/include/asm/octeon/cvmx-clock.h | 171 | 6d1c55766677 |
| arch/mips/include/asm/octeon/cvmx-dtx-defs.h | 13414 | 8f0e95766035 |
| arch/mips/include/asm/octeon/cvmx-gserx-defs.h | 11028 | a05442e6821a |
| arch/mips/include/asm/octeon/cvmx-pcie.h | 383 | e8448cb03355 |
| arch/mips/include/asm/octeon/cvmx-pcieepx-defs.h | 14083 | c4eb218bf247 |
| arch/mips/include/asm/octeon/cvmx-qlm.h | 350 | 14ff0a82559c |
| arch/mips/include/asm/octeon/cvmx-sriomaintx-defs.h | 5830 | 239a13db332e |
| arch/mips/include/asm/octeon/cvmx-swap.h | 130 | cf85470aa647 |
| arch/mips/include/asm/octeon/cvmx-tim-defs.h | 1816 | ced9fde272c6 |

## FFN-authored, NOT from the SDK

- `arch/mips/include/asm/octeon/ffn-cvmx-compat.h` — shim; cvmx.h must NOT be replaced
- `arch/mips/include/asm/mach-cavium-octeon/topology.h` — pcibus_to_node self-define
- `arch/mips/include/asm/octeon/cvmx-csr-enums.h` — 2 UART enums only, NOT the whole SDK file
- see `0002-ffn-authored-edits.patch`, `0003-gmxx-prototype.hunk`, `0004-qlm-gating-lines.txt`
