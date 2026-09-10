# FE100 parser and forwarding diagnosis

The working four-port SYSPORT fabric was recovered after this experiment.
Correct parser programming is implemented and readback-tested, but it is
**not retained in the active fabric**: it exposes an unresolved downstream
lookup stall. No DIRECT packet rewrite acceleration is claimed.

## Findings

The prior "no packets returned" result hid a useful distinction. Raw capture
shows every diagnostic packet returning with CMH type1, which the owner's
`pcs/packets/fe100.py` defines as NOTFLOW. The production decoder accepts
SYSPORT type3 and correctly rejects these as unforwarded traffic. Both
experimental EtherType and real unicast IPv4/UDP probes were checked. The
IPv4 destination MAC remained unchanged. This was not hardware rewriting.

Non-clearing counters show DIRECT LIF hits followed by FWD bypass. A snoop
capture using the owner's `pca_cap` mechanism confirms LIF FWDTYPE4, key31,
next-hop index31, but packet type0 and bypass mask255 even for IPv4. The chip
revision is `0x000a0001`, selecting the FE100/A1 PCA layout; FE101 offsets are
different. Invalid captures contain stale data and must not be interpreted.

`ffn_fe100_parser_inspect.py` then read all 55 owner parser entries. Every
entry was identical:

```
8000000080000000000000000000000000000000000000000000000000000001
```

That entry has zero parse action/type, despite the populated reference
`/mnt/clones/5220-sysroot1-full/opt/dpfs/etc/fe-parser.json` on the VM.
The earlier owner JSON call returned success without verifying its output.
A software-only probe parsing `{"v":9}` found numeric node type3 but zero at
offset40, where disassembled `ju_get_int` reads the integer. Thus the initial
type-code mismatch hypothesis was incorrect; numeric decoding/layout remains
the relevant incompatibility. The new path avoids the owner JSON helper.

## Implemented correction

`ffn_fe100_parser_apply.py` encodes the owner's JSON into the DWARF-verified
32-byte `pan_fe100_parse_entry_t` and uses the owner's table insert/fetch API.
It validates field widths, rejects unknown settings, canonicalizes masked
TCAM key bits, saves the original table, and compares every readback. Only
the known commissioning baseline may be replaced. A partial failure restores
all touched entries and reports any rollback failure.

All 55 entries programmed and read back correctly. Source SHA-256:
`6dcbd4fa1e12e5798a55bf22dded9f3fdfc5d0ee90d454d8a4ee88238a1bbddf`.
The first validation failure at entry36 was a normal masked-key canonicalization;
rollback passed before the encoder was corrected. Three codec tests pass,
including a fixture from the actual hardware readback.

With the corrected parser, captured IPv4 now has PT1, MODE1, EtherType0x0800
and bypass mask0. The DIRECT LIF still selects next-hop31. DFP and FWD did
not capture that packet, and the pipeline stalled. Restoring the parser
alone did not drain the queued work. This narrows the next investigation to
ACL/TLU and downstream lookup readiness; it does not prove a specific cause.

## Recovery performed

The MP fabric service was stopped and `fabric-four-disable` quiesced the
BCM routes. CP/DP/BCM were not rebooted. A copy of FE100 trace/state files was
saved on CP under `/var/backups/ffn-fe100-before-recovery-1789009398`.

The owner's two-write FE100 soft reset was followed by NIF/TMI initialization
and block initialization in this order:
TLU, PRW, LAG, QMM, LEF, FWD, DFP, ACL, LIF, PAR, CFP, EGR, IPQ, NIF, TMI.
The original commissioning parser behavior was restored for compatibility.
System-port mappings and four SYSPORT LIF entries were reinstated.

Crucially, the NIF receive mappings must also be restored after reset:

| BCM source port | FE100 logical port | LIF slot |
|---|---|---|
| 28 | 1 | 3 |
| 14 | 3 | 4 |
| 16 | 5 | 2 |
| 7 | 13 | 1 |

Missing these mappings initially produced `nif_rx_invld_port_lut_pkt_err`
and no downstream traffic. After restoring them, both 300-packet control
probes passed. The BCM routes and MP fabric were restarted. Thermal services
remained active throughout. All new next-hop/PCA/PMF test state was removed.
The older logical-port8 commissioning mapping and SYSPORT LIF slot0 were
also reinstated. The final L2/L3 matrix passed 1,200 expected forwards and
660 expected blocks, with the original native port configuration restored.

## Evidence and next step

- `DIRECT-NOTFLOW-DIAGNOSTIC-20260909.txt`: raw envelopes and 600-packet test.
- `DIRECT-NOTFLOW-COUNTERS-20260909.json`: non-clearing counter deltas.
- `PARSER-FIX-ATTEMPT-20260909.txt`: corrected classification and failed delivery;
  only snapshots marked valid are meaningful.
- `RECOVERED-PATH-20260909.jsonl`: restored physical control probes.
- `OFFLOAD-RECOVERY-MATRIX-20260909.jsonl`: final physical L2/L3 regression.

Next, establish ACL/TLU readiness in an isolated, quiesced commissioning
sequence before retaining the corrected parser. Do not install it at boot
or equate table readback with functional forwarding. Keep software fallback
and the proven SYSPORT fabric until packet tests pass.

## Follow-up: lookup health diagnostics

Installed `ffn_fe100_lookup_health.py` on CP. It takes bounded samples under
the table lock, selects only non-clearing counter aliases and specific status
registers, and separates current flow control from sticky flags. Packet and
lookup counter pairs are reported separately: equal lookup requests/responses
must not hide packets waiting between ACL and DFP. Samples are not atomic,
and counter deltas assume no reset during the sampling interval.

The live baseline has balanced ACL/DFP packet counters, no occupied ACL/TLU
queues, and zero ACL lookup requests during sampling. That confirms only
the existing compatibility path, not working ACL acceleration.

The external path has a separate unmet prerequisite. TLU tables 4, 5, 6 and
10 select external memory, while TDI's DRAM and TCAM clock status bits are
zero. TDI reports current TCAM-request flow control. Its reported queue
levels are retained as evidence but cannot be trusted as live occupancy
while the clocks are off. The owner's `pdt/fe100.py` diagnostic around lines
10301–10315 associates table2 with ACL and table4 with FWD. Table2 is internal
on this appliance; table4 is external. Therefore the TDI state is relevant to
forwarding acceleration but is not established as the cause of the earlier
ACL stall. No TDI configuration or parser activation was attempted here.

The JSON investigation also confirmed the loaded provider is the local owner
`3p/libcjson.so.1`. Owner DWARF describes an 80-byte node with `valueint` at40,
`valuedouble` at48 and `valueint64` at56. The earlier numeric probe stored9 in
`valueint64`, while `valueint` was0 and `valuedouble` was NaN. This is a numeric
conversion problem, not evidence of a struct-layout mismatch. Its underlying
cause remains unproven; the direct encoder remains the tested workaround.

Seven parser/health tests pass, including clear-on-read alias exclusion,
counter wrap, historical flags, and completed lookups with queued packets.
Both physical 300-packet compatibility probes passed with no corruption.
Fabric and both thermal services remain active. Evidence:
`LOOKUP-HEALTH-20260909.json`, `LOOKUP-HEALTH-PACKETS-20260909.jsonl`.
Full samples remain on MP in `/var/log/ffn-lookup-health-20260909.json`.
