# The BCM88375 TM-port header

Ports whose `tm_port_header_type_in` is `TM` — on this board **4, 5, 8, 9, 12, 17, 20, 24**, i.e.
every CP, DP and MP link — do not carry plain Ethernet. The chip parses the first four bytes of
every frame as a Traffic Manager header and takes the forwarding destination from it. Sending
ordinary Ethernet at such a port makes the chip read the destination MAC as that header:
`ff:ff:ff:ff` yields `snoop = 15`, and the frame is trapped as `bcmRxTrapItmhSnoop15`. That is not
a malfunction, it is the port doing exactly what it is configured to do.

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

Next hypothesis. `config.bcm` configures `dtm_flow_mapping_mode_region_<N>` for regions **65..128
only**, leaving the low flow-id space unconfigured, while our VOQ is auto-allocated at **qid 4**
(decode the returned gport `0x241c0004`: qid = bits[13:0], sysport = bits[25:14] = 112). A queue
below every configured region would never be valid. So allocate the VOQ with
`BCM_COSQ_GPORT_WITH_ID` at a qid inside a configured region and name that qid in the header.

Also unresolved: whether ingress and egress use the same structure at all. The egress header is
plainly `[dest16][src16]`; the ingress `snoop` evidence fits `dune_itmh_v3_s`. ITMH and OTMH may
simply be different headers.

`diag pp Frwrd_Decision_Trace` and `PKT_associated_TM_info` would settle it by reporting the resolved
destination, but they print nothing on this build — `Signal DB for qmx was not found`.

## Tools here

* `ffn_itmhsend.py` — send a frame with an arbitrary 32-bit header prepended.
* `ffn_tmrecv.py`   — capture and hex-dump what the chip puts in front of a frame on a TM out port.
