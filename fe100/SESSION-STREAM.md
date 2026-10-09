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
