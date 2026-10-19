# Continuous session observations

The MP supervises `ffn-fe100-session-feed.service` alongside controld. It reads
OCTEON DP conntrack events over pinned SSH and forwards each message to the CP.
The CP acknowledges the producer identity, sequence and synchronized inventory.
The MP publishes acknowledgement status only after checking that reply.

This is an observation path. It does not program FE100 tables, renew software
session leases, allocate NAT mappings or enable hardware admission. Production
offload still requires the existing independent policy and hardware qualification.

The producer requires an applied Security/NAT acknowledgement and validated
ownership grants. It subscribes before dumping conntrack, replays interleaved
events, and completes the snapshot before declaring readiness. Limits bound
frames, session count, bootstrap time and output blocking. Process/boot/stream
identities and ordered sequences prevent reuse after a restart. Relative monotonic
timestamps detect backlog without requiring synchronized plane wall clocks.

Policy, collector, route, neighbor or interface changes invalidate the snapshot.
Kernel event loss, malformed messages, stale transport or mismatched CP replies
also clear readiness. A new complete snapshot is required to recover. Missing
policy acknowledgement is continuously reported as unavailable; the authenticated
transport stays alive and retries without claiming any sessions are ready.

## Batched changes and the MP inventory

After `synchronized`, the DP producer no longer emits one message per conntrack
event. Each burst it reads (bounded at 50 ms / 2048 events) leaves as one
ordered `changes` message of at most 128 entries, `{"op":"upsert","row":...}`
or `{"op":"close","identity":...}`, coalesced to the latest entry per identity.
The relay forwards a batch to the CP in a single round trip, which is what lets
receipt stay inside the 90 s backlog budget under hundreds of events per second;
the per-event `upsert`/`close` forms remain accepted from a legacy producer.
`begin` may carry `limits` (`conntrack_max`, `stream_capacity`) so the MP can
state the table's capacity; receivers keep it out of the policy context.

Beside its status report the relay publishes `/run/ffn-fe100-session-inventory.json`
(root-only, rewritten at most every 2 s while the table changes and at least
every 5 s): the synchronized inventory as compact rows (tuples, NAT, rule, vsys,
TCP state, TTL, byte/packet counters, software/L3 offload verdicts and blockers,
first-seen time) plus counters. `management/session_inventory.py` is the reader
the core manager imports through `platform_mod('session_inventory')`: it feeds
`/api/dashboard/sessions`, `/api/monitor/sessions` (the WebUI Session Browser)
and the CLI's `show session info | all | id`, so every view counts the same
table. A report from a dead or restarted relay, or older than 15 s, is reported
as unavailable, never as an empty table; a not-ready report carries its reason.
Hardware admission is always reported false: this is observation data.

## Installation and status

The image overlay installs the shared protocol on both planes, the receiver on
CP and producer on DP. MP's `management/install-control-channel.py --identity
/absolute/key/path` installs the service and a systemd credential reference using
the selected plane identity. Its output identifies services requiring a restart.
Keys are never copied into the source or telemetry.

The existing controld `fe100-sessions` status resource includes
`continuous_stream`, including when on-demand policy observation is blocked.
CP agent policy telemetry also includes `session_stream`. Local reports are
`/run/ffn-fe100-session-relay.json` on MP and
`/var/lib/ffn/fe100/session-stream.json` on CP. Consumers check the report writer's
boot/PID/start identity and age; file presence alone does not mean readiness.
`cp_acknowledged` indicates transport acknowledgement, not hardware forwarding.

## Native validation

On DP, run `python3 /usr/local/sbin/validate_session_stream.py --run` as root.
The validator creates and removes a uniquely named isolated network namespace.
It uses fixture policy acknowledgement, real conntrack netlink events and two
loopback UDP packets; a route change must invalidate the stream. It never changes
the appliance's data namespace, customer configuration or FE100 tables.

The native MIPS64 run on 2026-09-29 passed snapshot bootstrap, kernel session
updates, ordered reception and route invalidation. The live MP/CP transport was
acknowledged while correctly reporting that the current Security/NAT generation
lacked an applied acknowledgement. This evidence does not qualify production
FE100 packet forwarding or NAT rewriting.
