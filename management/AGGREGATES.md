# Aggregate LACP and subinterfaces

The MP aggregate supervisor owns CP redirects and the OCTEON LACP process.
Negotiation uses the physical members of the parent AE, independently of its
VLAN subinterfaces. Layer 3 units use owned 802.1Q devices on the OCTEON aggregate
TAP. Static addresses and interface management profiles apply per child, with
independent receive/transmit counters. An applied child requires a fresh matching
configuration acknowledgement; saved XML alone does not establish readiness.
Malformed or unsupported units are withdrawn and reported separately from LACP.

The runtime tracks the complete running revision separately from member and
LACP settings. Addressing, management profiles, LLDP and subinterface commits
preserve the owner, physical links and LACP engine. Changes to members, member
speed, administrative state or LACP parameters require reconciliation.

Parent network updates are acknowledged by the DP using a configuration digest.
A separate worker applies IP and management-profile changes without blocking
LACP. Parent data delivery is paused until the matching update succeeds; failures
remain visible and do not claim an applied configuration. DHCP hooks are fenced
by owner token and network generation so a retired client cannot restore old
addresses. Unsupported VLAN attachments remain separate child failures.

AE None is a link-only parent (`aggregate-only=yes`), with root-level bond/LACP
settings and preserved child units. Admin State Down explicitly disables it.
Legacy Layer 3 bond/LACP settings are accepted. Physical Ethernet None still
disables the physical link. Parent Layer 2 forwarding is not implemented.

Stale CP/DP observations still withdraw collection and distribution and close
member links. Recovery is explicit through the MP controller; the system does
not blindly restart after uncertain hardware outcomes. An awaiting-address state
can coexist with negotiated LACP: the parent DHCP client has not received a lease.
Check `distributing` and individual member synchronization/collection/distribution
alongside physical link status.

Only configured single-tag VLANs enter the TAP. Unknown, priority-only and stacked
tags are dropped. Unit ID and VLAN tag are separate; units retain their netdevice
on address/profile updates. The aggregate TAP remains up for children when parent
mode is None. Ownership aliases protect unrelated interfaces from adoption or deletion.

Supported attachment scope: up to 64 Layer 3 units, static IPv4/IPv6 addresses,
MTU up to 1500 and no larger than the parent, and interface management profiles.
Layer 2 units, DHCP on children, TCP MSS options and virtual-router isolation are
not implemented. Parent and child routed transit remain default-deny until the
aggregate security-policy binding is implemented; this is not Internet forwarding.

`test-aggregate-vlan-linux.py` exercises the real kernel VLAN and nftables paths
in disposable namespaces: tagged ping, profile denial, address replacement,
stable netdevices, untagged isolation, transit blocking and unit removal. Packet
classification and stale acknowledgement tests run separately.
