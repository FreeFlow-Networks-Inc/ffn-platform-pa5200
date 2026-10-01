# FE100 native session counters

The PA-5220 emitted FLOWSTATS type 19 during isolated FE100 UDP port-NAT tests.
The older PCS diagnostic decoder's CONTROL/STATS_COUNTER branch is a different
format. It must not be used to decide that the live hardware emits no counters.

The sysroot's `condor_cmh_t`, `condor_flow_stats_t` and `condor_fcr_t` type
information and its native receive routine match the captured bytes:

| Offset from BCM RAW return | Field |
| --- | --- |
| 0 | Four-byte OTMH: configured trunk and FE100 return source |
| 4 | Four-byte retained ITMH |
| 8 | Sixteen-byte common header; type 19 at offset 11 |
| 24 | Eight-byte statistics header; record count at offset 27 |
| 32 | Eight-byte compact FCR records |

Each FCR contains a big-endian 32-bit flow ID, followed by a 32-bit word:
reason `[31:30]`, packet increment `[29:22]`, octet increment `[21:0]`.
The reference receive path **adds** these increments. Reason 2 contributes to
accounting but skips the session activity refresh. The parser preserves the
reason; it does not grant flow ownership or authorize a NAT lease extension.

The live capture contains `000003e9 50002040`: flow 1001, reason 1, 64 packets,
8,256 bytes. Four separately received copies account for 256 distinct 129-byte
frames, all verified against the independent translation/checksum oracle.
Identical records must not be deduplicated by their byte contents.

A second test installed both TCP NAT directions concurrently and interleaved
256 packets. Four hardware messages each contained two FCRs: flow IDs 1001 and
1003, each reporting 32 packets and 4,512 bytes. The native receiver therefore
accounted for 128 packets / 18,048 bytes per direction, exactly matching the
independent 141-byte packet oracle. The messages arrived in a continuous
capture; gaps between the injector's short capture windows did not lose counts.
This qualifies the compact batched record layout, not connection establishment
or full TCP state synchronization.

`octeon/native/ffn_fe100_stats.c` validates the scoped return envelope, message
type, count, complete record array and optional zero Ethernet padding. It
accepts the commissioned compact format only. Other FCR formats need separate
qualification. The native `ffn-fe100-stats-probe` is a bounded receive-only
observer with socket-drop and malformed-message reporting. It performs no
register writes, flow installs, packet forwarding or conntrack updates.
It checks the kernel's resetting drop counter during polling and before
publishing a delta; loss or malformed input ends the stream immediately.
Drop totals remain cumulative across those kernel reads. Output transport
supervision and timely owner withdrawal remain the coordinator's responsibility.
With `--health`, the native observer emits a sequenced heartbeat at least every
300 ms of idle polling and uses a nonblocking control pipe. A closed or blocked
reader ends the process; it cannot hang indefinitely holding a healthy-looking
receiver. Packet counts in its final summary exclude heartbeats.

`NativeCounterStream` accepts decoded control events from one trusted native
receiver. It accumulates deltas only for explicitly registered entries, rejects
gaps/replays and foreign hardware epochs, bounds ID storage, and never reuses a
retired flow ID in the same table generation. Unknown IDs cannot create state.
A stopped or failed receiver makes activity unavailable. Receiver health and
an unchanged snapshot are not evidence that an unobserved flow is idle.
Accounting requires a supervised `NativeCounterStream` with a heartbeat timeout
and an initial native health event. Its owner timer must call `sync()` during
idle periods. Missing health, elapsed receiver-clock drift, or queued stale
events fence the stream; a late heartbeat cannot resurrect it. The receiver
timeout cannot exceed five seconds. This is the accounting process's own
check, and does not replace the independent CP withdrawal watchdog.

`ffn_fe100_accounting.AccountedSession` coordinates these counters with the core
`dataplanes/ctlease` native kernel endpoint. It verifies both original/reply
tuples and their NAT outputs against the already-bound UDP connection. It
counts equal consecutive deltas separately, never refreshes merely because a
snapshot was read, and requires acknowledged hardware withdrawal before closing
the kernel lease. Delayed activity, a changed hardware epoch, missing counter
ownership, receiver failure or a kernel rejection fences the session. A failed
withdrawal retains the lease and requires recovery.
Idle synchronization validates the bound kernel object without adding counters
or refreshing its timeout, so deletion or expiry does not require another
hardware report to trigger withdrawal.

On the MIPS64 dataplane, an isolated conntrack namespace received live reports
from 256 paired UDP PNAT packets on the optical loop. Each direction reached
128 packets / 16,512 Ethernet bytes and the kernel reached exactly 128 packets /
14,720 L3 bytes, with activity refreshing the configured lease. The independent
wire oracle verified all 256 translations. These connection objects were test
fixtures; production conntrack state was not altered. The kernel tests also
prove that late reports cannot refresh a deleted/recreated tuple, duplicate
ownership is rejected, and reason-without-activity does not refresh the timeout.

Production still requires one durable owner for the receiver and flow-ID
generation, deployed attachment mappings, and an independent watchdog with
acknowledged withdrawal of hardware ingress/flows before a receiver or ownership
lease ends. TCP state synchronization is not supplied by UDP accounting.
The bounded probe and this evidence do not enable production admission.
