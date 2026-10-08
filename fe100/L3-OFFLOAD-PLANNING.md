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
