# BCM88375 SDK integration and verification

The PA-5220 uses the DPP/Jericho API path for its BCM88375. Consult the
[OpenBCM DPP trunk implementation](https://github.com/Broadcom-Network-Switching-Software/OpenBCM/blob/master/sdk-6.5.27/src/bcm/dpp/trunk.c),
[statistics API](https://github.com/Broadcom-Network-Switching-Software/OpenBCM/blob/master/sdk-6.5.27/include/bcm/stat.h),
and [L3 API](https://github.com/Broadcom-Network-Switching-Software/OpenBCM/blob/master/sdk-6.5.27/include/bcm/l3.h).
These are reference interfaces. The installed SDK's return codes and hardware
readback determine availability. No OpenBCM source is copied into this module.

## Control and evidence

WebUI and CLI use MP controld. The existing BCM status resource now returns
`sdk_reference`: chip identity, trunk capacity, L3 capacity reporting, and exact
64-bit counters for currently active faceplate ports. The CP's
`ffn_bcm_reference.py` uses the existing serialized BCM daemon; it starts no
second SDK instance and performs no forwarding/configuration changes.

Counter values are decimal strings to preserve precision through JavaScript.
Unsupported counters have `available: false` and `value: null`; they are never
reported as zero traffic. Missing, duplicate, truncated and echoed results fail
validation. `bcm_l3_info` returning success with zero capacities sets
`capacity_reported: false`; it does not qualify hardware routing.

## Current coverage

| Area | Implementation | Verification boundary |
| --- | --- | --- |
| PCI/register access and SDK DMA | `ffn_bcm`, `ffn_bde` | Loaded/bound on the CP |
| ASIC/SerDes initialization | persistent SDK process and Debian startup | Running BCM88375_B0 observed |
| Port enable, speed and autonegotiation | `ffn_bcm_link`, faceplate controller | SDK readback; copper uses separate external-PHY control |
| Copper PHY/MAC alignment | PHY controller and `ffn_bcm_copper` | Existing per-port mapping and controller evidence |
| VLAN membership and STP | BCM daemon operations | Available wrappers; full production L2 convergence is not established by their presence |
| Trunk create/read/delete | `ffn_bcm_trunk` | Empty-trunk lifecycle tested on hardware; member traffic uses the existing journaled aggregate owner |
| LACP-selected member updates/hash | `ffn_aggregate_bcm_lag` | Existing owner, expected-membership checks, system-port encoding and readback |
| Per-port counters | `ffn_bcm_reference.counters` | Native SDK readback on active copper and QSFP ports |
| L3 interfaces/hosts/routes | Reference capacity readback | Complete BCM L3 provisioning is **not implemented/qualified** by this patch |
| Stateful NAT/security hardware offload | FE100 session/path components | Production admission remains disconnected; SDK trunk support is not FE100 offload |
| QoS/ACL/mirroring/tunnel SDK coverage | Partial existing subsystem work | Not a complete OpenBCM implementation; requires per-feature ownership and hardware tests |

## Trunk corrections

The legacy daemon endpoints now use typed system-port GPORTs, read hardware
capacity, select PORTFLOW explicitly and verify complete readback. An existing
trunk is accepted only if membership order, member flags and hash selection
already match. It is never silently overwritten after `BCM_E_EXISTS`.
Destroy requires the caller's expected members and verifies absence afterward.
Failed writes report reconciliation required rather than claiming convergence.

On 2026-09-29, a live empty-trunk test selected an unused ID from the reported
capacity, journaled intent, verified create/idempotent-create/delete/readback,
and confirmed final absence. No ports were assigned to that test trunk. This
does not qualify member traffic, L3 routing, FE100 NAT, or line-rate performance.

## Deployment

The CP image includes both new modules. The SDK reference helper and BCM status
integration can be updated without restarting the ASIC. Updating the daemon's
legacy endpoint dispatch takes effect on its next planned restart; do not
restart the chip merely to collect counters. Existing aggregate controllers
continue using their separate, journaled member-update implementation.
