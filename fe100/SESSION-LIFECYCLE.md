# FE100 session lease component

`ffn_fe100_lifecycle.SessionLifecycle` wraps a single `PolicyOwner`. The future
trusted evaluator calls `start` only after software policy application and
producer identity verification. `start` and every event require an identity
containing `boot_id`, `pid`, `/proc/PID/stat` `process_start`, and a fresh
`stream_id` UUID bound to the authenticated synchronization. A boot ID alone
cannot distinguish daemon restarts or PID reuse. The transport must reject old
synchronization handshakes; these fields are continuity checks, not credentials.
It sends strictly ordered open/refresh/close
events plus periodic heartbeats. Refreshes require the exact decision digest;
they cannot keep a session beyond its maximum lifetime without reevaluation.

The owner loop must call `tick` independently of incoming events, with the same
journal/adapter locks used by admission. Expiry, lost sequence, producer restart,
policy replacement, changed bindings or qualification failure fences admissions
and deletes exact journal-owned entries. Unacknowledged deletes retain recovery
state. Monotonic leases are never adopted across process restarts.
Clock regression, nonfinite timestamps, malformed sequence numbers, duplicate
opens and incomplete producer identities also fence and drain existing leases.

## Paired NAT decisions

`PolicyOwner.admit` accepts the original legacy non-NAT decision or a paired
decision with `original` and `reply` TCP/UDP conntrack tuples. The paired shape
also requires `nat_digest` and `path_digest`; it omits the legacy
`src/dst/sport/dport/protocol/zone` fields. Both retain the exact Security
revision, digest, rule, allow verdict, established-state acknowledgement,
interface pair and inspection requirement. Unsupported inspection stays in
software. The NAT-required flag must agree with the actual tuple translation.

The embedding owner must supply a `paths(request)` callback that returns
`{nat_digest, directions}`. Its two directions each contain `ingress`, `egress`,
post-translation `destination`, ingress lookup `zone`, `next_hop`, and SHA256
`route_revision`, `neighbor_revision`, and `attachment_revision` values.
These are current, read-back resource-owner records, not raw route observations
or hardware indices supplied by a remote producer. The callback must change its
revisions and drain dependent sessions **before** replacing the referenced
hardware tables. Its complete snapshot digest must match the acknowledged
decision. The owner checks it before admission, again after pair installation,
and during every reconciliation, including heartbeat-only periods.

NAT additionally requires a separate `nat_qualified()` callback (default false).
The native adapter retains its independent NAT packet-qualification check.
Directional lookup zones require explicit SessionManager acknowledgement;
both actions still must reverse the exact translated tuples. Both directions
decrement TTL. Carrier readiness is an offload admission check here; it is not
a configuration validation or commit requirement.

A dependency change currently drains the whole owner's table conservatively.
Failure to delete either direction retains durable recovery intent and blocks
new admissions. The owner does not restore persisted sessions after restart.

This remains an internal component. It is not wired into the production CP
policy endpoint or live session source and does not change the running gate.
Keep the existing recovery timer until the persistent owner and independent
watchdog replace its responsibility with equivalent verified cleanup.

## Native FE100 table validation, 2026-09-29

`validate_nat_lifecycle.py --run` exercises the paired PolicyOwner and lifecycle
against the native CP FE100 session APIs. It uses two fixed benchmark tuples
in reserved zones, an exclusive table lock, empty-table preflight, bounded
native calls, and a separate durable `nat-lifecycle-lab.sqlite3` journal.
Rerunning first recovers that exact journal. It does not change packet parsing,
next-hop tables, BCM ports, customer routes or policy. Fixture bindings authorize
only the table test; they are not commissioned production attachments.

The live PA-5220 passed paired NAT action readback and removal for all five
triggers: close, expiry, NAT generation, neighbor generation, and restarted
producer. Evidence: `nat-lifecycle-validation-1790692696691377427.json` on CP,
boot `6ab40f86-8291-4375-9134-d26ece6b5ba5`. Cleanup completed and hardware flow
counts returned to zero. No packets were sent. This is table/lifecycle evidence;
it does not qualify simultaneous bidirectional packet forwarding, TCP state,
aggregate/VLAN attachments, statistics handoff, or production admission.
