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

`NativeCounterStream` accepts decoded control events from one trusted native
receiver. It accumulates deltas only for explicitly registered entries, rejects
gaps/replays and foreign hardware epochs, bounds ID storage, and never reuses a
retired flow ID in the same table generation. Unknown IDs cannot create state.
A stopped or failed receiver makes activity unavailable. Receiver health and
an unchanged snapshot are not evidence that an unobserved flow is idle.

Production still requires one durable owner for the receiver and flow-ID
generation, conntrack/NAT refresh on qualified activity, and acknowledged
withdrawal of hardware ingress/flows before a receiver or ownership lease ends.
The bounded probe and this evidence do not enable production admission.
