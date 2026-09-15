# PA-5200 vendor driver modernization

Analysis and first implementation: 2026-09-14. Reference directory on the
build VM: `/mnt/clones/ffn-vendor-drivers`. All 28 module files were inventoried
with `tools/audit_vendor_drivers.py`; the supplied SHA-256 manifest verified.
The full symbol/ELF report remains on the VM at
`/home/stephen/ffn-driver-audits/ffn-vendor-driver-audit-20260914.json`.

## Findings

These are compiled modules, not a buildable vendor source release. MP modules
target x86-64 Linux 3.10.88; OCTEON modules target big-endian MIPS64 Linux
3.10.87. Their old kernel ABI and private vendor dependencies prevent loading
them into FFN's 6.18 kernel. A GPL module tag alone does not provide source.
Vendor binaries and private reference libraries are not copied into this repo.

CP owns switch/FE100 configuration. DP owns packet processing and the legacy
LWM interface. MP owns chassis management and external administration. The
owner startup script explicitly disables `port_link` because its interrupt
path was not ready. Re-enabling that old IRQ code is not a modernization.

## Replacement matrix (all 28 modules)

“Candidate” means an implementation direction, not hardware qualification.
Existing FFN replacements still require subsystem-specific compatibility and
traffic tests; this audit does not certify them as complete vendor equivalents.

| Role | Legacy module | FFN replacement or remaining work |
| --- | --- | --- |
| MP | pcic | Audit existing FFN PCI transport against reset, mapping and lifetime semantics. |
| MP | pci_dma | New DMA lifecycle implementation needs descriptor/ownership documentation and target tests. |
| MP | if_pci | Integrate the FFN plane transport; qualify netdev queues, MTU and recovery. |
| MP | if_vif | Modern DP logical TAP assignments and MP controls implemented; see VIF-INTEGRATION-20260915.md. MP-host VIF transport remains separate work. |
| MP | fabric_vif | Supported TAP/bridge/VRF adapter connects VIFs to the DP packet transport. No legacy ixgbe override or MP-host ABI compatibility claimed. |
| MP | nac | Determine bound hardware and required ABI before implementing. |
| MP | cpld_wdt | Existing FFN CPLD access is a basis; watchdog must use a single owner and verified reset semantics. |
| MP | power_ctl | Existing FFN chassis controls are a basis; qualify each power/reset operation separately. |
| MP | crash_save | Standard persistent crash reporting candidate; remove vendor ext3/panic hooks. |
| MP | pan_wp | Use supported debugging facilities where sufficient; no direct replacement of IDT/private CPU hooks. |
| CP | fe100 | Extend FFN FE100 controls; replace ksysd callbacks with explicit FFN interfaces. Session offload requires verified table and DMA semantics. |
| CP | cfg_push | Implement versioned configuration transport with acknowledgement and DMA ownership; old cvmx DMA calls cannot simply be relinked. |
| CP | ce40 | Port only verified device operations to managed PCI resources and a versioned interface. |
| CP | port_link | New `ffn_port_events.py` provides bounded read-only polling, freshness and transitions. Implemented and live-probed. |
| CP | linux-kernel-bde | Existing `ffn_bcm`/`ffn_bde` provide a starting point; vendor ABI coverage is not complete by implication. |
| CP | linux-user-bde | Existing FFN BDE compatibility layer; audit ioctl layouts, user copies, DMA and teardown before expansion. |
| DP | lwm | New packet/notification transport required; investigate SSO group 110 and netlink semantics before replacing IRQ/queue code. |
| Shared | cavmodexp | Standard kernel crypto/userspace crypto candidate; acceleration requires verified OCTEON III support and vectors. |
| Shared | cn63xx_hfa | Do not enable based on module name; qualify actual OCTEON III accelerator and available SDK interfaces. |
| Shared | octeon-rng | Upstream OCTEON hwrng candidate; verify target configuration and device binding. |
| Shared | rng-core | Use target kernel hwrng core, not the legacy module. |
| Shared | ce10 | No PA-5220 binding established in this audit; leave disabled. |
| Shared | cheetah | No PA-5220 binding established in this audit; leave disabled. |
| Shared | ocelot | No PA-5220 binding established in this audit; leave disabled. |
| Shared | pan_wp | Supported MIPS debugging facilities candidate; investigate owner-specific behavior if required. |
| Shared | cn6xxx RapidIO | Determine whether any PA-5220 device requires it before porting. |
| Shared | 8250_pci | Target kernel PCI UART driver candidate; compare PCI IDs and board quirks. |
| Shared | xr17v35x | Target kernel Exar UART support candidate; compare clock, GPIO and interrupt quirks. |

## New port observation interface

`ffn_port_events.py probe` reads `port.list` through the existing BCM owner API.
`serve` atomically publishes `/run/ffn-port-events/state.json`; `status` rejects
expired observations. A BCM8375 PCI identity guard limits active operation to
the CP. This module belongs to the optional PA-5200 platform package.

The schema includes all 24 front ports, BCM IDs, administrative state, link,
effective carrier and speed. Effective carrier requires both admin-enabled
and observed link-up. Missing readings are unknown. Query failures clear old
observations; data expires after six seconds and across reboot. A generation
UUID and transition sequence allow consumers to detect restart and changes.
Readers must check generation, freshness and per-port null values. Consumers
must use `status` or equivalent expiry checks rather than trusting raw JSON.

This is an observation interface, not a replacement for packet RX/TX, physical
carrier propagation to DP netdevices, or LACP slow-protocol delivery. It reports
`lacp_transport_qualified: false`. No IRQ, register, port configuration, or
forwarding state is modified. Physical link does not distinguish an absent
cable from a connected but inactive/incompatible peer.

Validation: eight Python unit tests passed on the VM, including bounded
response size and total query deadline. A read-only probe on the
PA-5220 CP running `6.18.49-ffn-debian-cp-dirty` returned all 24 ports without
error: port 1 at 1 Gb/s; ports 5 and 13 at 10 Gb/s. All other ports reported no
link at that instant. These are observations, not end-to-end forwarding tests.
An eight-second polling run on the CP maintained sequence 1 while readings
were unchanged. After stopping it, `status` correctly marked all 24 readings
unknown/expired.

Follow-up integration: the CP service is now enabled and running, with zero
restarts at verification. MP daemon resource `port-events/status` invokes the
fixed CP `status` command. Authenticated GET endpoints at
`/api/system/runtime/port-events` and `/api/pa5200/port-events` expose it through
the optional platform adapter; no mutation is registered. The Interfaces page
shows a snapshot table and suppresses link/speed claims for stale observations.
The live MP RPC returned 24 fresh observations, with ports 1, 5 and 13 linked.
Sixteen management tests and the UI tests passed. The deployed patch preserved
the appliance's newer BCM/PHY controls; backups have the suffix
`.before-port-events-20260914`. An unauthenticated HTTPS request returned 401.

The LWM binary's symbols confirm a POW-group parameter, threaded interrupt,
skb queue and netlink delivery path. The existing FFN `dpnet2` transport is a
management link; its existence does not replace that DP packet/notification
path or establish front-port forwarding.

## Remaining acceptance work

1. Wire qualified observations into the DP physical-port backend without
   creating a second hardware owner. MP API/UI observation integration is done.
2. Establish DP RX/TX, metadata, DMA ownership, queue backpressure and reset
   recovery before adding LWM/FE100 acceleration. Test actual traffic and drops.
3. Build kernel replacements against the exact configured 6.18 target and
   matching Module.symvers. The old SDK 4.9 build defaults are not deployment
   instructions for the current kernel.
4. Qualify FE100 session programming and invalidation against CP configuration
   revisions; retain a software path until offload behavior is demonstrated.
5. Qualify UART, RNG, watchdog and crash handling independently. No blanket
   module-load or chassis reset is part of the audit.
