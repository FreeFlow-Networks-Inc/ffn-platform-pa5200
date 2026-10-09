# FE100 internal forwarding qualification

`validate_front_sessions.py --cross --vlan-return --mac-loopback --single-port`
uses a supervised BCM MAC loopback on the selected reserved test port. The MP
refuses configured test ports and checks the running configuration and aggregate
owner throughout. The CP journals its MAC settings, table entries and BCM
redirects before writes and restores them on completion or loss of supervision.

The packet-table fixture now installs a missing FE100 NIF receive port-map entry
for its reserved ingress/return port and reads it back. The owner ABI uses a
three-byte switch/device/physical-port key, an integer output pointer for fetch,
an integer logical-port argument for set, and only the key pointer for delete.
Those signatures were checked against the reference owner binary. Existing
conflicting mappings are rejected; previously absent mappings are deleted at
cleanup, including after a failed packet test.

A live internal test passed baseline receive, exact-match forwarding with VLAN
and destination-MAC rewrite, TTL decrement and checksum verification, explicit
drop, and withdrawal after session removal. FE100 counters confirmed the lookup,
forwarding and drop paths. All temporary changes were restored.

This test does not qualify an external cable, two-port transit, sustained load,
or production policy admission. Reports explicitly identify internal loopback
and keep external-wire and distinct-port qualification false for a single port.
Production session admission remains separately gated.
