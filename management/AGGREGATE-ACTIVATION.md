# OCTEON aggregate activation

The `aggregates` resource now supports `status`, `validate` and `apply` through
the MP control daemon. The WebUI and FFN-CLI use that same resource. Activation
uses the committed aggregate/member configuration, fenced by both the full
running XML digest and an integer controller revision. Candidate edits remain
staged until Commit. No client supplies a hardware command, MAC, interface path
or replacement network configuration.

## Operations

In Network > Interfaces > Aggregate Ethernet Status:

- **Qualify LACP** starts the CP/DP owners and enables the member links, but
  keeps collection/distribution closed and creates no network interface, DHCP
  client or route. It can observe and synchronize with a partner without
  advertising that the firewall is collecting or distributing data.
- **Activate** starts the software aggregate TAP, local interface management,
  configured addresses or DHCP, and LACP-gated ingress/egress. Stop a qualification
  owner before activating its network attachment.
- **Hardware Egress** is visible but disabled pending TM hash commissioning.
  Its backend implements CP-owned BCM trunk selection; the MP rejects the
  operation until physical distribution qualification is complete.
- **Stop** withdraws the DP owner, disables the physical members, removes owned
  redirects and restores their previous speed configuration.
- **Recover** also cleans up an interrupted owner using its saved CP/DP identity.

The equivalent CLI commands are:

```text
show platform aggregates
request platform aggregate ae1 negotiate
request platform aggregate ae1 deactivate
request platform aggregate ae1 activate
request platform aggregate ae1 offload
request platform aggregate ae1 recover
```

The generic authenticated `/api/system/planes` endpoint carries resource
`aggregates`, action `apply`, and payload fields `group`, `operation`,
`running_revision`, and `revision`. Read the revision fields from
`/api/interfaces/aggregate-status` immediately before the request. A successful
command response acknowledges starting the supervisor; use the subsequent
aggregate state to determine negotiation and address readiness.

## Ownership and withdrawal

`aggregate_activation.py` runs one MP supervisor per group. It starts the DP
owner with closed gates, then prepares the CP redirects and physical links.
The CP installs ingress redirects to the existing BCM24/OCTEON trunk before
enabling any member. It verifies queues, trunk header format and redirect
readback without allocating SDK resources or restarting BCM.

Fresh CP observations travel through the MP to the DP. Request sequence, owner
token, running configuration digest and CP/DP lifetime checks reject stale or
misrouted observations. The DP expires physical link leases independently of
the MP. Missing control input closes the owner; the CP watchdog disables links
before removing redirects. The DP watchdog removes a stale owner's tagged
netdevice and local network state. Failed cleanup remains visible and is not
silently adopted by another owner. A failed MP supervisor retries after ten
seconds. It fences the saved identity, recompiles committed configuration, and
requires a fresh DP acknowledgement. Stop remains stopped. Recovery after a DP
reboot includes restoration of missing packet-fabric stages and journaled BCM
queues for the committed members; recovery does not invent routing, addressing,
VLANs or member assignments.

Scoped packet owners share the fabric lock and exclusively own each physical
member. Existing legacy owners retain their exclusive fabric lock. The WAN
owner owns port 1 separately, so aggregate activation never attaches that port.
The first installation requires restarting the WAN packet process to load this
locking change; routine aggregate activation does not restart it.

`ffn_aggregate_datapath.Gates` uses the same permission map for readback and every
packet. Egress uses stable layer-2/3 rendezvous hashing across distributing
members. Fragmented packets retain the same member selection. LACP and LLDP
are sent to their physical member, never through the aggregate hash. Packet
inspection retains the physical ingress member and uses separate owner statistics.

## Scope and remaining limits

This is an **OCTEON aggregate with optional BCM egress member selection**,
not a line-rate throughput claim. Current activation supports optical ports 5–24,
2–8 members, Layer 3, MTU 576–1500, active/passive LACP, fast/slow timers,
minimum links, local management profiles, IPv4 DHCP, and LLDP advertisements.
DHCP changes only this aggregate's lease and explicitly requested default
route; it never flushes the routing table, replaces another interface's route,
or changes host DNS. An address or route application failure withholds readiness.

**Transit is default-deny.** A dedicated nftables guard blocks forwarded
traffic entering or leaving the aggregate until aggregate security-policy
binding is implemented. Local services follow the configured interface
management profile. Existing NAT/routing policy compilation is not claimed to
be integrated with the new aggregate. Layer 2 aggregates,
jumbo frames and hardware firewall-session offload remain outside this version.
Activated supervisors recover after MP, CP or DP restarts. Install the matching
DP packet module using the [packet-fabric recovery instructions](PACKET-FABRIC-RECOVERY.md).
Faulted engines, conflicting resources and uncertain allocations remain blocked.

### Hardware egress ownership

`ffn_aggregate_bcm_lag.py` creates a previously absent trunk whose ID matches
the aggregate number. The CP journals ownership before programming, checks the
256-group chip layout, and verifies PSC PORTFLOW and every member/flag after
each change. Existing trunks are never adopted. Watchdog shutdown disables
physical members before destroying the owned trunk.

Only the complete sorted member set or an empty trunk is programmed. A missing
LACP member empties the hardware trunk; the DP immediately falls back to its
software selector. A fresh CP acknowledgement must match the DP's current
distribution gates before any packet uses the hardware destination. LACP and
LLDP always target individual physical ports. No global hash configuration,
port header format, VLAN or queue allocation is changed.

Live testing showed that the SDK rewrites ingress source metadata to SPA
identifiers even with `BCM_TRUNK_MEMBER_INGRESS_DISABLE`; that flag also excludes
members from this device's ingress LAG resolution table. The commissioned path
uses flags zero and retains the redirect to OCTEON for inspection. The DP accepts physical
source IDs and the fixed SPA indices belonging to this owned group. Keeping the
hardware member set fixed prevents an index from being reassigned to another
physical port while packets are in flight. Unknown group/index IDs remain
unmatched. This mapping applies to LACP as well as data packets.

The implemented offload scope is **egress member selection only**. Packet inspection,
interface management, LACP state and transit enforcement remain on OCTEON.
Hardware TX counts are aggregate totals; per-member software TX counts are not
presented as hardware per-port measurements.

API and packet-header references: Broadcom's
[DPP trunk implementation](https://github.com/Broadcom-Network-Switching-Software/OpenBCM/blob/master/sdk-6.5.27/src/bcm/dpp/trunk.c),
[logical system port encoding](https://github.com/Broadcom-Network-Switching-Software/OpenBCM/blob/master/sdk-6.5.27/src/soc/dpp/ARAD/arad_ports.c),
and [ITMH layout](https://github.com/Broadcom-Network-Switching-Software/OpenBCM/blob/master/sdk-6.5.27/include/soc/dpp/headers.h).
The appliance SDK and wire readback, rather than API presence alone, determine
which operations are enabled.

**Load-balance key.** The trunk port receives TM-header (ITMH) frames, which
the ingress pipeline does not parse, so the switch's own LAG load-balance key
is one constant and every flow left through one member. The commissioned path
therefore carries the key: the native DP owner folds the same flow fields its
software selector hashes (addresses, VLAN, protocol, never transport ports)
into eight bits and writes them into bytes 1..3 of the RAW_DSA area of every
hardware-egress frame; a direct-extraction field entry preselected on the
trunk port (`ffn_aggregate_bcm_lag.py`, group/presel/qualifier 12, extraction
offset 104) copies that byte into the LAG LB key before the destination is
resolved. The switch strips the area before faceplate transmission but its
egress program reads it as a DSA tag: byte 0 holds the tagged flag (a set flag
makes it rebuild a VLAN tag from the other bytes and the LAN switch drops the
frame) and the upper four bytes hold PCP/VID, so those five stay zero. The DP
trunk driver admits bytes 1..3 and refuses any envelope with byte 0 or the
upper four set (`octeon/kctl/ffn_dp_trunk.h`). The field program is one shared
resource: installed and verified before the first owned trunk is created,
verified with every readback, removed after the last owned trunk is destroyed.
The DP uses the LAG destination only while the CP acknowledgement carries the
verified program (`lb_key`); an unverified readback is a software-selection
condition, not a fault.

**Current commissioning result (2026-10-08):** the key path is built and
verified end to end except for the last step, which this SDK does not take.
Proven on the appliance with the SDK's own per-port counters: hardware egress
delivers LAG-destined frames and the LAN answers through it (pings 10/10 on both
members' peers), the DP trunk driver now admits the key bytes, the field program
installs and reads back, and the frames reach the LAN intact. Not achieved: the
switch applies **no user field program at all** to packets of a TM-header port
(a probe entry redirecting every port-24 packet to the loopback port had no
effect while an identical entry is accepted and read back), so the extracted key
never reaches the LAG resolver and the chip's own key stays the constant 257;
all 128 distinct test flows left through one member. Setting the port's header
type to INJECTED at run time is refused by this SDK (`BCM_E_UNAVAIL`), so
hardware distribution needs a configuration-level change of the injection
envelope (INJECTED-type trunk port, re-initialisation, DP encoder and driver
changes), which is a separate re-architecture. Hardware activation therefore
stays blocked in the MP API/CLI and WebUI with that reason; the key path, the
driver relaxation and the field program ship dormant behind the gate for that
follow-up. Software selection remains the production mode and was re-verified
after every window (both members distributing, LAN reachable both ways).

Changing the committed XML withdraws a running owner; activate again against
the new revision. `configd` reports the actual owner/member application state
instead of claiming that saving a profile activated hardware. It does not
silently start an aggregate for the first time during installation.

## Installation

Install the following platform sources in the existing selected runtime:

- CP `/usr/local/sbin`: `ffn_aggregate_hardware.py`, updated `ffn_faceplate.py`,
  `ffn_aggregate_bcm_lag.py`,
  and the existing `ffn_copper_forwarding.py`/`ffn_wan_forwarding.py` dependencies.
  Install and enable `ffn-aggregate-watchdog.timer` with its service.
- DP `/usr/local/sbin`: `ffn_aggregate_runtime.py`, `ffn_aggregate_datapath.py`,
  `ffn_aggregate_offload.py`, `ffn_aggregate_vlan.py`,
  executable `ffn_aggregate_dhcp.py`, and updated LACP, inspection and WAN owner
  modules. Retain the existing packet transport/fabric and core interface
  management libraries. Install and enable `ffn-aggregate-dp-watchdog.timer`.
- MP platform directory: `aggregate_activation.py`, `aggregate_backend.py`,
  `aggregate_config.py`, `configd_applier.py`, and `cli_extension.py`. Install
  `ffn-aggregate@.service`; merge the `aggregates` commands from `planes/mp.json`
  into the existing MP worker configuration without replacing other resources.
  Reload systemd and the MP worker after installing. Core WebUI changes supply
  the lifecycle buttons.

Keep backups and preserve the appliance's unrelated installed customizations.
The daemon validates the presence of both watchdog timers before activation.

## Verification

Controller tests cover revision fencing, committed configuration compilation,
member ownership, redirect-before-enable ordering, rollback, watchdog expiry,
stale tokens and normal watchdog/heartbeat lock contention. Packet tests cover
real collection/distribution gates, fragment-consistent hashing, member
withdrawal and qualification that never advertises collection/distribution.

The Linux interoperability harness uses the production software gate driver
against Linux bonding in isolated namespaces. It tests bidirectional IPv4,
LACPDU timeout, renegotiation and forwarding with either member down. It does
not prove the physical BCM/OCTEON wire path or throughput.

Live qualification additionally verified CP redirect/admin readback and
withdrawal when the MP supervisor was paused: the DP owner exited and the CP
watchdog disabled both members and redirects. WAN addressing, routes, NAT and
the DP boot lifetime were unchanged. Both physical vPC members subsequently
negotiated at 40 Gb/s with matching system/key, distinct partner ports, and
collection/distribution enabled. The isolated native MIPS64 DHCP harness also
verified address/default-route installation and withdrawal. The live aggregate
was awaiting a DHCP offer when this implementation was commissioned.

Physical software-selection verification sent 128 distinct IPv4 test flows:
member counters increased by 63 and 65 packets. With either member temporarily
disabled, a further 128 packets left exclusively through the surviving member;
both members rejoined afterward. These bounded tests verify selection and
withdrawal, not throughput or end-to-end internet forwarding.
