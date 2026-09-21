# Policy interface bindings

Install `ffn_platform_policy_bindings.py` on the DP with
`python3 install-policy-bindings.py`. Core NAT must include the optional platform
binding selector. The existing commissioned physical VIF map is retained.

Aggregate and VLAN bindings are discovered from current DP owner observations.
The adapter checks root ownership, a five-second monotonic freshness bound,
boot identity, process start time, LACP gates, network readiness, parent and child
aliases, VLAN tag, parent link and applied child state. Missing evidence withdraws
the binding. A new acknowledged owner is discovered without editing a static
mapping file. No appliance addresses or VLAN configuration are embedded in code.

Core compilation excludes an unaddressed aggregate transport parent when it
carries subinterfaces. A parent with an IPv4 address or enabled DHCP remains a
routed endpoint. Zone membership and the child configuration remain unchanged.

Tests cover stale/replaced owners, VLAN/parent mismatches, ownership changes and
renewed bindings. The live candidate NAT rule passed DP validation after deployment.
The native NAT namespace suite also passed. The production Security transit guard
remains in place; NAT validation alone does not authorize traffic.

## Security kernel candidate

The current DP kernel has conntrack labels/events but lacks `NF_CT_NETLINK`.
An external module build failed at modpost because `nf_conn_pernet_ecache` is not
exported by that kernel. Do not force-load or bypass symbol validation.

`build-security-kernel.py` copies an explicitly selected source tree into a new
output directory, enables the required kernel interfaces, builds an image and
matching in-tree modules, and records a manifest. It verifies that the original
configuration and symbol inputs remain unchanged. It does not select a boot image.

A kernel was built on the VM with release
`6.18.49-ffn-debian-dp-security-20260921+`. Its image SHA-256 is
`8aca1befb85fd70ebf087a0d3f67056d38954be7a39da294fcae658ca9e992e3`.
The final build includes the NAT FIB modules required by existing boot services.
`build-security-drivers.py` rebuilt the external
packet, link and crypto drivers from build directories whose prior module hashes
exactly matched the live DP modules. Their new ABI/release and module dependencies
were checked and included in the candidate module tree.

On 2026-09-21 the operator requested the DP restart. The CP boot service selected
the hash-verified image while preserving the prior kernel and module tree.
Debian/systemd, all 40 CPUs, SSH, the DP agent and the external drivers returned
with zero failed DP services. The aggregate owner obtained a new boot-specific
acknowledgment, both 40G members resumed distributing, and its VLAN attachment
returned without changing candidate or running XML. Conntrack NEW and DESTROY
netlink events were verified in a disposable namespace. Native NAT and Security
packet suites passed on the new kernel; candidate NAT preflight also passed.

The session event collector and coordinated Security/NAT apply provider remain
unfinished. The successful restart does not commission Security rules or remove
the production aggregate transit guard.
