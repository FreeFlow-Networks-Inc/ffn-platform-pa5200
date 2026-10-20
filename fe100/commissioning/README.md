# FE100 session commissioning: one real session through the chip

These tools put exactly one live conntrack session into the FE100 session table and
carry it in hardware for a bounded window, on the production ports, with the DP's
software path otherwise untouched. They are the harness behind the first session
forwarded in hardware on the PA-5220 (2026-10-19, see "Evidence" below); they are
not the product's offload path, which will be the FE100 control service admitting
sessions from the observation feed.

## Why the DP diverts the session instead of the switch steering it

The faceplate ports are RAW front ports with force-forward to the DP trunk. A PMF
redirect on such a port does not steer (confirmed again on 2026-10-19; the
September DISTINCT-PORT-ATTEMPT note records the same), and changing a production
port's header type is not a non-disruptive change. So the DP itself diverts the
tuple: `steer_dp.py` installs tc flower drops for the two directions on the LAN VLAN
device and the WAN device, taps the frames before the IP stack, and re-injects them
through the packet trunk to the lab front port (13, bcm7), whose MAC loop feeds the
FE100 NIF exactly like the qualified lab injector. Egress rules on switch port 3
(`steer_cp.py egress-create`) match the FE100's DSA bytes and the destination MAC and
redirect each direction to its real system port: the WAN port and one fixed LAG member.

## Files

| file | runs on | role |
| --- | --- | --- |
| `first_session_mp.py` | MP | orchestrator: picks the session from the inventory, builds the plan, drives the CP controller, the BCM steps, the diverter, samples counters, unwinds everything in reverse |
| `first_session_cp.py` | CP | controller (`--serve`): programs rx/tx port maps, LEFs, SMAC, next hops, LIFs, queue maps, ACLs and the NAT session pair through the vendor library and `ResourceTables`; journals every write; `remove`/`restore` undo in reverse with ownership checks |
| `steer_cp.py` | CP | BCM operations for the window: egress rules, force-forward get/set, CPU-message capture on port 20 |
| `steer_dp.py` | DP | the per-tuple diverter (tc flower drops + re-injection through the trunk) |
| `ct_liberal.py` | DP | the hand-back fixup: marks the conntrack entry TCP be-liberal in both directions and refreshes its timeout through ctnetlink |
| `ffn_dp_ssh.py` | MP | DP ssh wrapper (`bash -c "..."` for shell commands) |

Stage the CP files on the CP, the DP files on the DP and `ffn_dp_ssh.py` on the MP,
all under the same directory (default `/tmp`, override with
`FFN_COMMISSIONING_STAGE`). Run on the MP:

```
python3 first_session_mp.py --target <destination IP> [--flow-host <LAN host>] [--window 120]
```

The orchestrator waits for the flow to become an L3-ready candidate in
`/run/ffn-fe100-session-inventory.json`, then: MAC loop on the lab port, baseline
and session-path (force-forward 7 -> 3), controller prepare + install, egress rules,
CPU capture, divert + conntrack fixup, samples every 10 s (FE100 counters, conntrack
packets, divert counts, captured CPU messages), then unwinds. The report lands in
`/var/log/ffn-fe100-first-session-<ns>.json`.

## What the runs taught

**Queue map word 2 is the next-hop index.** The lab's IPv4 queue-map entry is
`(0x02020000 | queue, (ingress << 6) | 1, hop)` followed by the post-NAT addresses;
the third word is TCAM-matched (mask 0xffff). Every lab run used 31 because its next
hop was 31. With next hops 20/21 and the word still 31, every hit raised egress
exception 17 and the frame was punted to the CPU path (runs 1-6). The exception
arrives on the MP capture as: retained ITMH (4 bytes), common header (16, type at its
byte 3), message information (8: flow id, code in its last byte), packet information
(8), then the un-rewritten frame.

**A session in hardware needs its conntrack entry kept liberal and alive.** While
the chip carries the flow the DP sees none of its segments: conntrack's TCP window
goes stale and its unacknowledged timeout (300 s) runs out. On hand-back every
segment is `ct state invalid` and the WAN input chain drops it; run 7 stalled for
five minutes until the server closed. The kernel's own flowtable sets
`IP_CT_TCP_FLAG_BE_LIBERAL` on teardown; `ct_liberal.py` does the same through
ctnetlink (both directions) and refreshes the timeout. Applied at divert start in
run 8: no stall through the window or the hand-back.

## Evidence (run 8, 2026-10-19 19:37, window 120 s)

| sample | FE100 nif rx | flow hits | egr forward | nif tx | CPU frames | DP conntrack (orig/reply) |
| --- | --- | --- | --- | --- | --- | --- |
| t=60 | 1936 | 1936 | 1936 | 1936 | 0 | 591 / 2598 (frozen) |
| t=120 | 3223 | 3223 | 3223 | 3223 | 0 | 591 / 2598 (frozen) |
| after hand-back | | | | | | 646 / 2859 (moving) |

The rate-limited download on the LAN host advanced at its full rate through the whole
window and the hand-back. Cleanup errors: none; the box returned to its pre-run state
(tables removed, egress rules deleted, port 20 header restored, port 7 force-forward
restored, MAC loop released).
