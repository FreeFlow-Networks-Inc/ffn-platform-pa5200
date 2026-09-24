# FE100 — access, survey, and the route to bring-up

**Status 2026-09-21:** register access, initialized lookup/session memory, and
bounded physical forwarding/drop tests are implemented. Production policy
admission remains blocked. See [physical session qualification](PHYSICAL-SESSION-OFFLOAD-20260915.md),
[front-port forwarding limits](FRONT-EGRESS-POLICY-20260915.md), and
[automatic session recovery](POLICY-RECOVERY.md). The initial survey below
describes the September 5 starting point.

The FE100 is a Palo Alto ASIC — PCI `feed:fe1c`, class `0x020000` (Ethernet
controller), BAR0 = 1 MB — on the control plane's own PCIe bus. It is not the
BCM88375; it is a separate part on a different bus. The native Debian CP now
binds it to the `ffn_fe100` PCI driver.

## What now works

`ffn_fe100.py` runs on the control plane, maps the bound PCI `resource0`, and
reads or writes registers by name from the 5951-entry map recovered from the
vendor's `libpandp_cp.so`.

Two things it gets right that are easy to get wrong:

**The byte swap is not optional.** The control plane is MIPS64 big-endian and
this CSR window is not, so every 32-bit read comes back reversed. Reading
without the swap does not fail — it silently returns a different number. The
swap is confirmed by four registers matching their documented reset values
exactly:

```
nif_rst_ctrl        +0x10010 = 0x000fffff
nif_p0_mac_pcs_cfg  +0x1001c = 0x00049190
nif_p1_mac_pcs_cfg  +0x10040 = 0x00049290
nif_cr_imp_chk_en   +0x10508 = 0x0000000f
```

**Memory decode has to be on, and its absence is invisible.** The device ships
with the COMMAND register's memory-space bit clear. Reads then return
`0xffffffff`, which is indistinguishable from a register that genuinely reads
all ones. The tool checks the bit and says so rather than printing plausible
nonsense. The PCI driver's probe enables memory decoding and claims BAR0.

The Python reader and C owner adapters require the FFN binding; neither falls
back to `/dev/mem`. The adapter's register allowlists and write gates still
apply. PCI ownership does not replace the userspace table lock or the
commissioned initialization and session owners.

## Native PCI binding

Build `octeon/kctl/ffn_fe100.ko` against the exact CP kernel. The CP image builder
includes it, and `ffn-fe100-pci.service` loads it before FE100 link setup.
Probe validates the PCI identity and 1 MiB memory BAR, claims the resource, and
enables memory decoding. It does not reset FE100, reinitialize tables, enable
bus mastering, or claim forwarding readiness. Unbind only after stopping all
userspace owners and closing their BAR mappings.

On the CP, inspect the binding and readiness with:

```sh
systemctl status ffn-fe100-pci.service
ls -l /sys/bus/pci/drivers/ffn_fe100/
python3 /usr/local/sbin/ffn_hardware_verify.py
```

The CP agent reports the kernel binding, BAR presence, and a successful
non-clearing read separately. A module file or PCI device alone never counts
as responding. Physical forwarding and hardware NAT qualification remain
separate from binding, register access, and session-table validation.

## Survey: all 23 blocks answer

Every one of the 5951 registers reads, none unreadable, and exactly the 23
expected block prefixes appear — independent confirmation of the 0x8000-stride
block map that was recovered from the vendor tooling by a completely different
route.

```
block  total  nonzero      block  total  nonzero
prw      696      224      dfp      213      111
cfp      458       37      lif      179       88
nif      452       64      qmm      178      109
ipq      442      161      acl      174      108
par      438      180      lag      161      111
flu      428       85      lef      160      108
tlu      295       33      fhm      140       43
sem      269       66      fcm      135       39
tdi      259       91      prom      91       53
tmi      244       69      egr       57       15
fdt      237       78      hif       19        6
fwd      226       86
```

Only 9 registers chip-wide read all-ones. The values are reset values: the chip
is powered and responding, and nothing has configured it.

`par -> lif -> acl -> flu -> fwd -> prw -> egr` is a
parse/lookup/forward/rewrite datapath, which is what a session-offload engine
looks like.

## The route to bring-up, and why it is a diff

The bring-up sequence is known in SHAPE — `pan_fe100_nif_40g_init` is eleven
read-modify-writes at 0x100 stride with waits between, then interrupt work, a
PLL/link poll, a second RMW and poll, then a clear-interrupts — but **not in
values**.

An earlier attempt to recover the offsets and values statically from
`fe100_reg_wr` produced numbers matching no register in the map, because that
function is a logging wrapper whose arguments are biased into a string table.
**Those numbers were wrong and must not be reused.**

So: snapshot the chip, let the vendor code run its init, snapshot again, diff.
That yields ordering and values without interpreting anyone's disassembly.

`--snapshot` and `--diff` are that tooling. FFN can do the snapshot half today;
the missing half is a host that can run the vendor init once.

### The diff has a two-register noise floor

Measured, because a diff is only useful if you know what changes on its own.
Two snapshots three seconds apart, with nothing happening:

```
2 registers changed of 5951 compared
  flu_gtimer      0x000b2d85 -> 0x000b2d96
  sem_sem_timer   0x000b2d87 -> 0x000b2d97
```

Both are free-running timers. **Everything else in a bring-up diff will be
signal.** A baseline capture of the unconfigured chip is kept at
`/opt/ffn/fe100-snaps/reset-state.json` on the control plane.

## Known links to the rest of the board

Two BCM88375 ports reach this chip: port 3 (100G CAUI, through the Sesto
gearbox) and port 20 (ILKN4, 12 lanes over fabric quads 2/3/4). The vendor's own
`config.bcm` comment on the Interlaken lane order — "the FE100 guys swap on
their side" — is what identifies port 20's far end as this part.

## Update: 9 September 2026

Gearbox, NIF and TMI initialization now succeed on the PA-5220. BCM reports
both FE100 physical links up. See [measured bring-up and startup](LINK-BRINGUP-20260909.md).
The earlier missing-runtime discussion above is historical; native Debian
can run the owner's selected routines through the bounded FFN adapters.

## Packet forwarding update

Owner-derived SPM mapping and a scoped LIF SYSPORT rule now return 3,000
checked IPv4/UDP payloads through NIF, the packet pipeline, TMI and BCM to
the MP, with loopback off and no missing, duplicate or corrupt payloads.
LIF hits and ingress exception code zero confirm the selected forwarding.
The MP probe temporarily enables rx-all for the FE100 message envelope.
DP reception, firewall policy/offload and automatic packet-table startup
remain unfinished. See [packet-path evidence](PACKET-PATH-20260909.md).
