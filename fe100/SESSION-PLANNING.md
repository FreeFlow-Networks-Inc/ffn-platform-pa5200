# Acknowledged DP session observations

`show platform fe100 sessions [json]` asks MP controld for the read-only
`fe100-sessions/status` resource. The MP worker obtains a fresh, nonce-bound
kernel observation from DP over pinned SSH and sends it to the CP planner.
Console CLI uses its existing authenticated Unix control channel. No WebUI
process, configuration commit, dataplane restart or hardware insertion is needed.

The core `ffn_session_feed.py` requires current Security/NAT acknowledgements,
the durable rule generation, and an unchanged collector boot/process/recovery
identity before and after a subscribed conntrack dump. Lost/interrupted dumps,
policy changes, journal faults and observations exceeding eight seconds are
rejected. MP additionally limits the complete exchange to fifteen seconds.
IPv4 output is capped at 128 sessions with an explicit total and truncation flag;
a dump/replay exceeding 8192 entries fails. This is an on-demand observation,
not an ordered lifecycle subscription or a promise that a session remains live.

Only confirmed, assured, replied TCP/UDP sessions with a current explicit grant,
established TCP state, positive lifetime and one unambiguous interface pair can
be software candidates. Actual kernel original/reply tuples determine both
directional NAT translations. The planner preserves applied interface indexes
and owner aliases. It does not guess FE100 zones, next hops, flow IDs, VLANs,
routes or NAT allocations. Missing hardware attachment, lifecycle invalidation,
packet qualification and accounting remain explicit blockers. Every response
reports hardware admission disabled.

Installation: core plane installation includes the feed and decoder. Install
`ffn_fe100_session_plan.py` on CP beside the live-session status modules, and
`management/session_backend.py` on MP. The platform MP command manifest and
control-channel installer register the status-only resource. Reload the MP
execution worker after changing its command manifest; no DP restart is required.

## Isolated NAT qualification

The existing front-port harness now accepts `--nat address` or `--nat port`
alongside `--cross --vlan-return`. `--front5` exercises the reverse NAT tuple.
Only use this with the documented isolated front5/front13 loop. Test addresses,
VLAN4000, table reservations and ports are confined to commissioning fixtures;
they are never production defaults or user configuration.

The expected-frame oracle independently recomputes full IPv4/UDP checksums,
checks TTL decrement and exact tagged output, and retains miss, hit, drop and
removal phases with native counters and cleanup verification. The return-flow
cleanup key follows the translated tuple. CP readiness is checked before MP
changes capture features, creates BCM rules or sends test packets.

Initial live validation found no qualifying calibration/initialization journals
for the CP boot. The preflight refused the test with no BCM/capture changes or
packet injection, and cleanup was verified. Packet tests remain blocked until
the commissioning sequence is validated for that boot. Prior-boot records cannot authorize writes. Fixture
and codec tests do not prove live NAT rewriting. Even successful directional
UDP tests will not commission general production NAT: TCP, paired lifetime,
aggregate/VLAN attachment, counter handoff and restart recovery remain required.
