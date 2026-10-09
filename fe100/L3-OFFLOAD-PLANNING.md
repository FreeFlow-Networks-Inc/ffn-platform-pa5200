# L3 hardware offload integration

`show platform fe100 sessions json` now includes a per-session `l3` observation
from the OCTEON DP. MP controld relays a nonce-bound observation to CP; WebUI
and CLI use the same control resource. The CP helper uses the commissioned
owner library environment, including Debian SQLite, just like the existing
hardware and policy helpers.

The core `ffn_l3_offload.py` resolves both directions from current main-table
routes, acknowledged interface ownership, operational links, and confirmed
neighbor entries. The destination after conntrack translation determines the
forward lookup; the original source determines the reverse lookup. Output
includes the logical and Linux interface, owner/index, gateway or on-link
neighbor, Ethernet addresses, VLAN/parent, MTU and required TTL/MTU/fragment
exceptions. No appliance addresses, VLAN choices or table allocations are
embedded in the implementation.

Firewall-local destinations remain on the interface-management-profile path.
Missing, failed or incomplete neighbors, policy routing, VRFs, multipath,
unsupported route attributes, changed ownership and unsupported attachment
kinds block planning. A neighbor in any kernel-valid state (reachable, stale,
delay, probe, permanent, noarp) keeps its link address and its plan: under
forwarded traffic the kernel gets no transport confirmation, so entries cycle
through those states every reachable_time, and the snapshot projects neighbors
to destination, device, link address and validity so that cycle changes
neither the plan nor the generation digest. The DP producer drains a route
notification and ends the generation only when the projected topology differs.
Snapshots before and after planning must agree. Collection shares the existing
eight-second session-observation budget. Missing or changed route evidence
does not withdraw the software policy; it marks hardware planning unavailable.

The continuous session stream now includes the same directional next-hop plans.
DP subscribes to route, neighbor, address, rule and link notifications before
collecting topology. Every session plan carries the generation's topology
fingerprint. Notifications, netlink loss, policy changes and producer restarts
invalidate the entire generation; MP and CP discard its session observations.
An event queued while a plan is calculated prevents publishing that plan.
Both receivers validate interface ownership, directional destinations, unicast
MACs, VLAN, MTU and required exceptions. CP acknowledgements include the topology
fingerprint and L3 candidate count; mismatches fence the relay. Legacy peers
remain observation-only and cannot report `l3_observed`.

This is **next-hop planning, not hardware activation**. The feed uses conservative
whole-generation invalidation, not an atomic transaction with hardware table
withdrawal. CP keeps hardware admission disabled. Production activation still requires:

- Connecting generation invalidation to acknowledged hardware withdrawal before
  replacing an attachment or next hop, including admission lease expiry.
- Owned FE100 LIF/LEF/next-hop allocations and BCM steering for the actual
  aggregate/VLAN path, including membership changes.
- Simultaneous bidirectional physical forwarding, TTL-expired, MTU/fragment,
  policy withdrawal, aging/accounting and restart recovery qualification.

The internal resource-backed lease controller now joins
`SessionLifecycle` with `PathSessions`: it drains exact flow entries before
freeing next hops and source MACs, and checks a trusted generation token before
and after table calls. Its bounded leases and durable recovery are exercised
by the native table lab. The live observation relay still does not invoke this
admission controller; commissioned attachment mappings and the commit
withdrawal barrier remain required before production integration.

The supervised CP owner now dry-runs that gate chain over the inventory the
relay holds (`ffn_fe100_admission.py`): per session it builds the paired
admission request and the path-owner plan from the DP's L3 directions, the
applied bindings and the resolved attachment intent, validates them with the
production validators (tuple encoding, distinct bindings, inspection and
establishment requirements, NAT qualification, next-hop shape) and reports the
reasons that remain, separated into per-session blockers, generation-wide
reasons (policy activation, qualification, flow-ID allocator, and whether the
relayed configuration digest matches the owner's commit barrier; the DP's own
policy generation is fenced by the intake, not compared here)
and the commissioning items (zone and miss path, LIF/LEF, flow-ID namespace,
next-hop leases). It evaluates at most 128 sessions per status call and bounds
its projection to 16 KiB of the RPC envelope. The `status` response carries it
as `admission`, and `show platform fe100 sessions` attaches it as `supervised`.
Nothing is installed by the evaluation: `installed` is always zero and
`hardware_admission` stays false. A session that reports no blocker is
admissible only once the generation and commissioning items are closed.

Existing isolated FE100 tests establish packet rewriting under their documented
conditions. They do not authorize diverting production traffic into hardware.
The interface/VLAN configuration must already agree with the upstream network;
this planner never changes it to match observed traffic.

## Open items measured on the PA-5220 (2026-10-08)

With the projections and the producer recheck installed, a 16 MB download
through the appliance kept the continuous stream ready for 45 seconds with the
session held; six short sessions in 30 seconds caused exactly one generation
restart, at the moment the Security collector recorded their grants (rows
appear only then), with the reason "Current Security/NAT acknowledgement is
required": `runtime.status()` is transiently not acknowledged while grants are
recorded and the producer's context check ends the generation. That flap is the
next stream item. Independently, every session on this appliance is
`interface-pair-ambiguous` in the DP's own candidacy gate because its WAN zone
holds two interfaces; `assess` requires exactly one rule interface pair, so the
egress has to be resolved from the route (translated destination) and the
ingress from the original source before any session can become a candidate.

## Measured after the interface pair followed the routes (2026-10-08, late)

With the DP planner selecting the pair from the routes (FFN-NGFW
`codex/l3-neighbor-validity`) and the receivers accepting `l3.pair`, the live
inventory on the PA-5220 held 128 sessions of which 97 were software and L3
candidates, every one resolved to ae1.69 to ethernet1/1. The supervised
evaluation then named what remains per session: NAT rewrite not qualified (all
97, every LAN-to-WAN session is translated) and aggregate hardware egress not
commissioned for ae1.69 (all 97); generation-wide, policy activation,
front-port qualification and the flow-ID allocator. The 31 non-candidates were
TCP sessions not yet established or not yet assured by conntrack. So the gates
to a first admitted session on this appliance are, in order: the flow-ID
namespace, NAT packet qualification, and aggregate egress selection.

The flow-ID namespace was commissioned the same night (`FLOW-ID-OWNERSHIP.md`):
the control owner's status reports it and the admission evaluation's
generation list is down to policy activation and front-port qualification.
