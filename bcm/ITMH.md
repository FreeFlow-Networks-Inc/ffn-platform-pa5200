# The BCM88375 TM-port header

For the subsequently verified OCTEON/copper path, including distinct injection
and return envelopes, see [Copper forwarding](COPPER-FORWARDING.md). The
experiments below describe earlier investigation and their original limits.

Ports whose `tm_port_header_type_in` is `TM` — as the vendor ships them, **4, 5, 8, 9, 12, 17, 20,
24, 25, 26**, i.e. every CP, DP and MP link — do not carry plain Ethernet. The chip parses the first four bytes of
every frame as a Traffic Manager header and takes the forwarding destination from it. Sending
ordinary Ethernet at such a port makes the chip read the destination MAC as that header:
`ff:ff:ff:ff` yields `snoop = 15`, and the frame is trapped as `bcmRxTrapItmhSnoop15`. That is not
a malfunction, it is the port doing exactly what it is configured to do.

> **Check the live class before trusting any result below.** Ports **5, 8 and 24** were later moved
> to `ETH` for L2 bridging, and that change is hand-applied — it does **not** survive a switch
> re-init. `BCM-CONFIG-20260914.json` shows 5 and 8 as `ETH` in both directions. Every experiment in
> this file was run when port 5 was still `TM`; repeating one on an `ETH`-classed port yields a
> confusing non-result rather than an error, which is why `ffn_itmhsend.py` now verifies the ingress
> port's class and refuses by default.

## The header layout

Recovered from the vendor's own DWARF (`libpandp_cp.so`, `struct dune_itmh_v3_s`, 4 bytes) with
`tools/ffn_dwarfstruct.py`:

    [31:30]  2   type
    [29]     1   mirr_dis
    [28:27]  2   dp
    [26:8]   19  dst_prt
    [7:4]    4   snoop
    [3:1]    3   tclass
    [0]      1   ext

The `snoop` position is confirmed against live silicon by the `Snoop15` trap above, so this is the
real layout and not merely a plausible one.

## What the chip itself emits

Ports 4, 5 and 20 are also `tm_port_header_type_out=TM`, so the switch **builds** a header on egress
there. Forwarding between two of them and capturing on the far side makes the silicon show its own
encoding rather than leaving it to inference:

    5 -> 4 :  00 04 00 05  ff ff ff ff ff ff  02 b6 7d ad 14 00  0800 ...
    4 -> 5 :  00 05 00 04  ff ff ff ff ff ff  02 b6 7d ad 14 01  0800 ...

`[dest port : 16][source port : 16]`, followed by the original frame untouched. Only one field moves
between the two captures, which is what makes the reading safe.

**To capture these you must put the receiving interface in promiscuous mode.** With a TM header
prepended the leading bytes are not a MAC the NIC recognises, so its hardware filter drops the frame
before any socket sees it — `AF_PACKET` with `ETH_P_ALL` is not sufficient on its own.

## Status: ingress destination not yet solved

Injecting a hand-built header on port 5 is still rejected — `IqmRjctQnvalidErrPktCnt`, 100% of
frames — for roughly 1300 values tried: `dst_prt` 0..1023 across all four `type` values,
`[dest16][src16]` for dest 0..40, and explicit candidates including the silicon's exact shape with
the destination swapped, the dense port index, the VOQ gport's embedded sysport, and `ext=1` forms.

The header **is** parsed: prepending one flips port 5's counters from `snmpIfInNUcastPkts` to
`snmpIfHCInUcastPkts`, because the MAC the chip sees shifts by four bytes. The destination it
resolves simply never names a valid queue.

**The sweep could not have hit it.** The vendor's `usr/share/broadcom/dsa_tag_support.c` documents
the field: `dst_prt` is an L2 Destination, and an L2 Destination is *tagged*.

    |8|7|6|5|4|3|2|1|0|9|8|7|6|5|4|3|2|1|0|    19 bits
    |0|0|1|      System-Port-Agr          |
    |0|0|1|0|      System-Port            |
    |0|0|1|0|0|0|0|0|0|0|0|0|0| Dest-Port |

Bits **[18:16] must be `0b001`**. Every value in the 0..1023 sweep leaves them `0b000`, so the whole
sweep sat outside the encoding's valid space. `dst_prt = 0x10000 | port` — for port 13,
`itmh = 0x01000d00`.

**This confirms rather than discovers.** [Copper forwarding](COPPER-FORWARDING.md) records the same
word already validated on hardware on 2026-09-16 — *"four bytes `01 <BCM destination BE16> 00`"* —
so the open status recorded above is stale for the commissioned path. The vendor source only
supplies the *reason* the 0..1023 sweep could never have worked. `ffn_itmhsend.py --dest-port N`
now encodes it so the wrong form is not the easy one to reach for.

Note also what the copper work found alongside it: BCM15 had **no VOQ bundle at all**, and "a
synchronised PHY/MAC and an enabled port cannot substitute for queue resources, scheduler
connections and an ingress destination". The header was never the only missing piece.

Still worth testing afterwards, and unaffected by the above. `config.bcm` configures
`dtm_flow_mapping_mode_region_<N>` for regions **65..128 only** (modes 0/1/2, not set/unset:
65-68=0, 69-98=1, 99-128=2), leaving the low flow-id space unconfigured, while our VOQ is
auto-allocated at **qid 4** (decode the returned gport `0x241c0004`: qid = bits[13:0],
sysport = bits[25:14] = 112). A queue below every configured region would never be valid. Note the
vendor's shipped `config.bcm` has the *same* 65..128 range and forwards fine, so this cannot be what
separates their chip from ours — but it does imply their VOQs live inside a configured region. So
allocate the VOQ with `BCM_COSQ_GPORT_WITH_ID` at a qid inside a configured region and name that
qid in the header.

Also unresolved: whether ingress and egress use the same structure at all. The egress header is
plainly `[dest16][src16]`; the ingress `snoop` evidence fits `dune_itmh_v3_s`. ITMH and OTMH may
simply be different headers.

`diag pp Frwrd_Decision_Trace` and `PKT_associated_TM_info` would settle it by reporting the resolved
destination, but they print nothing on this build — `Signal DB for qmx was not found`.

## Tools here

* `ffn_itmhsend.py` — send a frame with an arbitrary 32-bit header prepended.
* `ffn_tmrecv.py`   — capture and hex-dump what the chip puts in front of a frame on a TM out port.

## Verified: the header is 4 bytes at offset 0

The first claim of this was unsound. The probe frames began `ff:ff:ff:ff:ff:ff`, so a header read at
offset 0, 1 or 2 all sees `0xffffffff` and reports `snoop = 15` regardless — the observation could
not distinguish them.

`ffn_offsetprobe.py` plants a *different* snoop nibble at each candidate offset (byte 3 = `0x30`,
byte 5 = `0x70`, byte 7 = `0xb0`) and lets the chip name the one it read:

    Committed_snoop: bcmRxTrapItmhSnoop3        snoop 3 = byte 3 => header at offset 0

So layout and position are both settled. Note `diag pp Frwrd_Decision_Trace` *does* print the
Committed_snoop and Considered-trap sections despite the missing signal DB; only the
resolved-destination part is lost.

## Verified: ingress and egress headers are different structures

Feeding the chip its own emitted word back in settles it. The 4 → 5 capture produced `00 05 00 04`;
injecting exactly `0x00050004` at port 4 (`in=TM`, with a VOQ built for port 5) was rejected —
`IqmRjctQnvalidErrPktCnt` for all 30 frames, zero enqueued.

`[dest16][src16]` therefore describes only what the chip **emits**. Sweeping that shape on ingress
cannot work, which accounts for a large share of the failed attempts.

## Withdrawn: the flow-region hypothesis

`dtm_flow_mapping_mode_region_*` governs **queue connectors**, and our connector is allocated at id
16 — far below the configured regions 65–128 — yet traffic flows through it. More decisively, **qid 4
is a demonstrably valid queue**: `force_forward` drives real traffic through it. "Queue not valid" is
therefore not a statement about the queue; the ITMH destination simply never resolves to it.

Which sharpens the open question rather than answering it. The config says what the field means:

    # Set the Base Queue to be added to the packet flow-id
    # when the Flow-Id is set explicitely either by the ITMH
    flow_mapping_queue_base.BCM88650=0

so queue = flow_id + 0, and `dst_prt = 4` ought to hit our VOQ. It does not, for any `type` and with
`ext` either way. That contradiction is the thing to resolve next.

One lead not yet eliminated: `bcmRxTrapVlanTagDiscard` appears in every Considered-trap list at
**trap_strength 4**, above the ITMH snoop's 3, and the injected frames are untagged. `Committed_trap`
reads "Not valid", so it is not plainly firing — but it is the only strength-4 trap present and is
worth ruling out before blaming the destination field.
