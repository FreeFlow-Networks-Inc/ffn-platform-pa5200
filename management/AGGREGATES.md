# Aggregate LACP and subinterfaces

The MP aggregate supervisor owns CP redirects and the OCTEON LACP process.
Negotiation uses the physical members of the parent AE, independently of its
VLAN subinterfaces. A Layer 3 `units` container is accepted by the parent intent
compiler; each child is reported separately with `applied: false` and an explicit
unsupported attachment reason. Configd reports that failure against the child
name rather than describing parent LACP as unsupported.

The runtime tracks both the complete running revision and a parent revision.
The latter excludes only that aggregate's currently unimplemented units and their
imports/zone/router memberships. A commit limited to these units can advance the
reported running revision without restarting parent negotiation. Changes to
members, parent addressing, management profiles or policy still invalidate the
owner and require reconciliation. This exception must be revisited when VLAN
attachment is implemented; active VLAN settings will then require reconciliation.

Stale CP/DP observations still withdraw collection and distribution and close
member links. Recovery is explicit through the MP controller; the system does
not blindly restart after uncertain hardware outcomes. An awaiting-address state
can coexist with negotiated LACP: the parent DHCP client has not received a lease.
Check `distributing` and individual member synchronization/collection/distribution
alongside physical link status.

An existing VLAN interface in candidate/running XML is not proof of packet
forwarding. Tagged AE attachment remains unsupported; parent transit remains
default-deny until its security-policy binding is implemented.
