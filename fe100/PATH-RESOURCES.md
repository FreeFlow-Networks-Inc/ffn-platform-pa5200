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

Source-MAC and next-hop operations now run through the C driver
`octeon/native/ffn_fe100_resources.c`, installed on the CP as
`/usr/local/lib/libffn-fe100-resources.so`. Python handles process supervision,
framing, library hash verification and the ownership journal. The C driver
owns mapping, register scope, typed vendor calls and the native watchdog. It
checks the inherited lock's inode, restricts every operation to its supplied
pool, uses aligned entry storage, and loads the exact open library descriptor
that Python verified. A register-scope fault permanently fences that worker;
an uncertain table operation requires readback before another write to its slot.
The driver allows one table scope per process and offers no reset operation.
The same driver now supports explicitly reserved LIF/LEF entries through
`AttachmentOwner`; see `ATTACHMENTS.md` for its separate ownership contract
and the remaining production wiring requirements.

The CP image build installs this driver automatically. An incremental deployment
must install the shared object before updating `ffn_fe100_resource_tables.py`;
there is no fallback to the previous Python vendor-ABI calls when it is missing.

IPv4 queue maps now use the same C driver (`qmap4`, 84-byte native union with a
36-byte IPv4 view). Pools are explicit and restricted to the reference-verified
first 32 slots; this is not the hardware capacity. Fetch and insert use the
three-argument owner ABI; delete explicitly selects IPv4 table 1. The driver
restores the selector omitted by hardware readback and rejects IPv6 selectors
or nonzero data outside the IPv4 view before making a hardware call.

The physical commissioning harness uses this driver for SMAC, next-hop, LIF,
LEF and QMAP operations. Its durable journal and independent recovery guardian
still withdraw ingress and remove both session directions before restoring
dependent tables. Queue IDs come from BCM readback and queue matches use the
post-NAT addresses. These mappings are broader than a five-tuple: a production
queue owner must arbitrate overlapping selectors and tie their lifetime to BCM
queue allocation, policy, attachment and routing generations before admission.
Production queue ownership and steering are not enabled by this change.

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

The C adapter was deployed and verified on 2026-10-01 using the same 12 checks,
with all resources removed afterward. See `NATIVE-RESOURCES-EVIDENCE.json` for
the hardware report and installed-library hash. Host and emulated MIPS64 ABI
tests also cover pool boundaries, lock mismatch, missing symbols, uncertain
writes and permanent fencing after a register-scope fault. Host ASan/UBSan
checks passed. This deployment did not restart the CP owner or packet workers.

The native QMAP extension subsequently passed the same 19 resource/attachment
checks and an isolated paired UDP port-NAT physical loop on ports 23/24. All 64
frames matched the expected MAC/IP/port rewrite. After an injected owner exit,
the independent guardian drained the sessions and restored the dependent
tables; all 64 subsequent frames followed the original path with no stale NAT
rewrite. Capture drops were zero. Repeated BCM ingress drain also passed after
the separate port owner restored its disabled baseline. This was not a rate test.

The first crash test exposed a journal-adapter bug: deletion incorrectly carried
snapshot bytes into an index-only native call. That attempt was recovered with
readback, and the adapter now strips delete payloads while the resource boundary
rejects malformed requests before starting a worker. The complete physical test
was repeated successfully. `NATIVE-QMAP-EVIDENCE.json` records both attempts,
installed hashes, regression checks and production limitations.
