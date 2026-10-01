# FE100 attachment table ownership

`AttachmentOwner` owns LIF ingress selectors and per-member LEF egress entries.
It accepts an interface name, a verified binding generation, logical ports,
zone ID, VLAN ID and an independently commissioned miss next hop. These values
come from the trusted hardware/configuration coordinator, not port names parsed
inside the driver. There are no default customer interfaces, VLANs or zones.

The C resource driver programs LIF/LEF entries using the reference-derived ABI
and encodes their wire fields. Every LIF matches both the logical ingress port
and all 12 VLAN bits. VID 0 represents untagged/priority-tagged traffic; it is
not a wildcard for every VLAN on a physical port. LEFs reference individual
logical egress ports. The owner can allocate entries for a selected member list,
but does not implement aggregate egress selection or hashing.

The owner requires explicit reserved pools, an exclusive hardware table backend,
a FULL synchronous journal, a trusted current-plan reader, and ingress-withdrawal
and dependent-flow/path-drain callbacks. It installs LEFs before LIFs and verifies
every write. Snapshots include both mappings and a digest of the owned resources.
They explicitly report `hardware_admission: false`: table presence is not proof
of a working packet path. Physical carrier is not part of this table contract.

Changes to the binding generation, member list, VLAN or zone invalidate the
mapping. Removal first requires acknowledged ingress withdrawal, then acknowledged
removal of dependent sessions and next hops. It removes LIFs before reclaiming
LEFs. Restarts recover exact journaled entries instead of adopting them. Foreign
entries, changed hardware boot identity, a failed drain or an uncertain write
leave recovery required. A port/VLAN selector cannot be owned by two attachments.

LIF/LEF allocation is currently restricted to the reference-verified first 32
indices, subject to the caller's explicitly commissioned subset. This is not
the advertised table capacity. Table 0, parser state, RX/TX port maps, queues,
BCM steering, exception routing and selected-member eligibility require separate
commissioning. This owner is not yet connected to production configuration apply
or the supervised session admission pipeline. No public activation API is added.

## Verification

`validate_attachments.py --run --ports <first>,<second>` performs table-only checks
on two explicitly selected, administratively disabled optical ports. It uses lab
LIF28/29 and LEF30/31, requires empty hardware flow tables, changes no link or BCM
settings, sends no packets, and records cleanup in a separate durable journal.

The 2026-10-01 appliance run passed seven checks, including tagged and untagged
readback, binding replacement, required dependent-flow drain, restart recovery,
lost write acknowledgement and withdrawal-before-drain ordering. All test entries
were removed and both selected ports remained disabled. The existing 12 paired
NAT/path resource checks also passed with the extended C driver. See
`ATTACHMENT-TABLE-EVIDENCE.json`. These results establish table programming and
ownership ordering; they do not establish production forwarding or throughput.
