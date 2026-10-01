# Hardware path resources and paired sessions

`PathOwner` journals both directional source-MAC and DIRECT next-hop entries
before native writes. It allocates only from explicitly commissioned pools,
skips occupied slots, verifies readback and excludes hardware-generated ECC
from next-hop payload comparisons. It neither allocates nor configures LIFs,
lookup zones, BCM redirects, routes or customer interface settings.

A trusted current-path resolver supplies route, neighbor, attachment and NAT
generations plus already commissioned LIF/zone mappings. A changed generation
invalidates the path. Removal requires a flow-drain acknowledgement before deleting
next hops and then source MACs. Conflicting hardware is preserved for inspection.
Restarted owners must recover durable intent before admitting new paths; a changed
boot never authorizes deleting an occupied slot merely because its bytes match.

`ResourceTables` provides bounded native source-MAC/next-hop operations using the
pinned FE100 owner ABI and inherited exclusive table lock. Separate worker processes
keep table access isolated from the session engine's register scope. Timeouts and
malformed responses require readback before another mutation of the affected slot.
There is no flush, reset, initialization or default production allocation range.

`PathSessions` connects those resources to `PolicyOwner` paired NAT sessions. It
assigns path digests from verified readback, drains sessions before releasing their
resources and recovers session intent before path intent after a restart. Invoke
its reconciliation periodically alongside the existing producer lease timer.
This is an internal trusted-evaluator component, not a public allow-rule API.

## Verification

`python3 /usr/local/sbin/validate_path_resources.py --run` requires the commissioned
CP, the normal native-library environment and empty hardware flow tables. It uses
the reserved lab slots 30/31 only. It sends no packets and changes no front-port,
parser, LIF, BCM or customer configuration. Durable journals are separate from the
production policy journal. Failed or ambiguous cleanup remains recorded.

The native MIPS64 FE100 run on 2026-09-29 passed:

- Paired source-MAC and next-hop installation, readback and removal.
- Neighbor-generation invalidation.
- Refusal to reclaim resources without acknowledged session drain.
- Durable restart recovery and recovery after a lost native write acknowledgement.
- Paired NAT session installation using the owned next hops, followed by ordered
  session and resource removal on a neighbor change.

All test resources were removed and hardware flow tables were empty afterward.
On 2026-10-01 the extended native run also passed resource-backed lease close,
idle expiry, heartbeat expiry, topology replacement, neighbor replacement and
producer restart. All 12 checks passed, including readback that both session
directions were gone before each resource deletion. The CP evidence is
`/var/lib/ffn/fe100/resource-validation-1790867085610052785.json`;
`cleanup_verified` and `after_flows_empty` are both true. No packets were sent.

This verifies table programming and lifecycle ordering, not production packet
forwarding. No production endpoint activates this owner yet. Still required are
applied Security/NAT acknowledgement, trusted live attachment/LIF/zone commissioning,
connection of the authenticated session stream to the trusted evaluator, counter
and logging handoff, exception handling and bidirectional packet qualification.
Production admission remains disabled until those conditions are verified.
