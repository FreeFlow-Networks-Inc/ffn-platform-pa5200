# L2/L3 forwarding through VIFs

The PA-5200 adapter exposes validated VIF settings to the shared Linux route
engine without transferring ownership of their netdevices. Generic FFN keeps
its existing physical-interface behavior. Core runtime commit
`11386877aca78dc00860d24f76c3a0b37396b9c2` or a descendant is required.

Configure a virtual router through **Dataplane Routing**, assign enabled L3
VIFs to that router through **Virtual Interfaces**, and start their transport.
The Routing page lists configured L3 VIF names and accepts them in its existing
JSON editor, for example:

```json
{
  "revision": 61,
  "routes": [
    {"dst":"0.0.0.0/0","dev":"fv1","via":"198.18.0.2","table":1001}
  ],
  "rules": [
    {"from":"198.18.0.0/24","iif":"fv1","table":1001,"priority":101}
  ]
}
```

Use the observed network revision. Routes and rules are complete replacement
lists; retain any existing entries. The gateway must be reachable using the
VIF's addresses, and the route table must match its VRF. IPv4, IPv6, default
routes and ECMP share the existing route validation and rollback behavior.
Adding routes or rules requires an attached, owned VIF with matching settings;
an inactive TAP or pending VIF recovery does not qualify. No socket call is made
while holding the network lock, avoiding a lock cycle with the VIF writer.

VIF reassignment rejects dependent saved routes and rules, including rules
observed in the kernel. Remove those dependencies before changing port, VLAN,
mode, addresses, VRF or enabled state. Configuration changes retain revision
checks. A VIF cannot be adopted through the physical `ports` configuration.

L2 VIFs bridge when their bridge VLAN/PVID matches. Wire VLANs may differ:
the VIF transport strips the ingress tag and inserts the egress binding's tag.
Different bridge VLANs isolate traffic. Wait for STP convergence after attach.

`management/install-vif-routing.py` installs staged core/adapter/runtime files
and adds the route guidance to the existing WebUI, preserving other installed
controls and backing up replacements. It requires empty stopped VIF assignments.
The MP remains the control path; packets traverse the DP PKI/SSO/PKO transport
and Linux VIF netdevices. FE100 production session admission remains disabled.

`test_vif_kernel.py` verifies Linux bridging, VRF forwarding, static routing,
ingress policies, TTL/checksum changes and dependency protection in a temporary
namespace. `management/validate-vif-forwarding-live.py` controls the physical
5--13 DAC test. `validate_vif_forwarding.py` injects unique frames from DP and
captures their exact physical return after bridge/router traversal, using
different ingress and return VLAN selectors to prevent recirculation. The live
harness restores routes, rules, VIFs, VRFs and saved front-port settings.

This remains a copying userspace transport at MTU 1500. Physical LACP,
line-rate forwarding and cold-boot restoration of VIF-dependent routes are not
qualified. The route configuration persists, but base networking must exist
before VIF devices, and VIF devices must exist before their routes are replayed.
This change does not enable a boot orchestrator or leave lab assignments active.

## Physical results, 2026-09-15

`VIF-L2-L3-FORWARDING-20260915.json` records the MP-controlled test on the
5--13 DAC with the updated shared engine installed on Debian/OCTEON:

| Test | Each direction | Result |
| --- | --- | --- |
| L2 bridge with wire VLAN translation | 4/4 exact frames | Passed |
| Different bridge VLANs | 4/4 ingress, 0/4 forwarded | Isolation passed |
| IPv4 VRF static route and ingress rule | 4/4 exact frames | MAC rewrite, TTL 63, checksum passed |
| IPv6 VRF static route and ingress rule | 4/4 exact frames | MAC rewrite, hop limit 63 passed |
| Disable a VIF with route/policy dependencies | Rejected | Assignment preserved |

These are sequential directions with both interfaces configured concurrently.
The initial routing run used permanent test next-hop neighbors. Tests compare
the complete expected frame and physical ingress metadata, not TX counters alone.
Cleanup restored empty assignments, no test routes/rules/VRF, stopped transport,
and ports 5/13 disabled at their saved automatic speed setting. Revisions advanced
to network 64 and VIF 15; existing physical-port network settings were preserved.

The optional `--neighbors` harness run creates fresh VIFs without permanent
neighbors. Its probe emulates only the reserved next-hop addresses, replying
to observed ARP requests and IPv6 neighbor solicitations over the DAC. It requires
successful neighbor exchange and exact physical routed frames in both directions.
