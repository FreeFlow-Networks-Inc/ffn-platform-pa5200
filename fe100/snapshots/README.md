# FE100 register snapshots

Full-chip reads of the FE100's 5,951 named control registers, taken with
`ffn_fe100.py --snapshot` on the PA-5220's control plane. Measured data from our
own hardware; no vendor content.

| file | taken | state of the box | result |
|---|---|---|---|
| `fe100-ce40-live-20261006.json` | 2026-10-06 | CE40 socket running the **a00d** personality (ce40.bin programmed, Linux booted with no Octeon reset in between); `ffn_fe100` bound; **before** the `CE40_ILKPHY_CFG=0x72` write | **2,145 live**, 3,754 zero, 52 all-ones |

"Live" means neither `0x00000000` nor `0xffffffff`. For contrast, the same read
on 2026-09-18 and 2026-09-20 — with the socket on the a101 image — returned
**0** non-zero words out of 262,144, and `prom_chip_rev_num` read zero. In this
snapshot it reads `0x01000a00`, byte-identical to the healthy 2026-09-04
reading, and the free-running timers (`flu_gtimer`, `sem_sem_timer`) advance by
`0x0a000000` per second.

This is the first FFN-owned snapshot of a *live* FE100: every earlier live
reading was inherited from an FPGA left loaded by a vendor boot. The register
map it is keyed on is the 9.0.x DWARF extraction (`fe100-csr-map.txt`), the
larger of the two maps we hold.

Use `ffn_fe100.py --diff <before> <after>` to compare against later snapshots;
the two timers are the measured noise floor, everything else is signal.
