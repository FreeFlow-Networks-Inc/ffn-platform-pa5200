# Aggregate LACP and subinterfaces

The MP aggregate supervisor owns CP redirects and the OCTEON LACP process.
Negotiation uses the physical members of the parent AE, independently of its
VLAN subinterfaces. A Layer 3 `units` container is accepted by the parent intent
compiler; each child is reported separately with `applied: false` and an explicit
unsupported attachment reason. Configd reports that failure against the child
name rather than describing parent LACP as unsupported.

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

An existing VLAN interface in candidate/running XML is not proof of packet
forwarding. Tagged AE attachment remains unsupported; parent transit remains
default-deny until its security-policy binding is implemented.
