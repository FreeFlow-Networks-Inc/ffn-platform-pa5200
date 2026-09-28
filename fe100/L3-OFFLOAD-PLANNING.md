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
Missing or stale neighbors, policy routing, VRFs, multipath, unsupported route
attributes, changed ownership and unsupported attachment kinds block planning.
Snapshots before and after planning must agree. Collection shares the existing
eight-second session-observation budget. Missing or changed route evidence
does not withdraw the software policy; it marks hardware planning unavailable.

This is **next-hop planning, not hardware activation**. A snapshot is not a
route/neighbor invalidation subscription. CP validates the directional binding
and keeps hardware admission disabled. Production activation still requires:

- An ordered route/neighbor and session lifecycle feed, with generation fences
  and withdrawal before replacing an attachment or next hop.
- Owned FE100 LIF/LEF/next-hop allocations and BCM steering for the actual
  aggregate/VLAN path, including membership changes.
- Simultaneous bidirectional physical forwarding, TTL-expired, MTU/fragment,
  policy withdrawal, aging/accounting and restart recovery qualification.

Existing isolated FE100 tests establish packet rewriting under their documented
conditions. They do not authorize diverting production traffic into hardware.
The interface/VLAN configuration must already agree with the upstream network;
this planner never changes it to match observed traffic.
