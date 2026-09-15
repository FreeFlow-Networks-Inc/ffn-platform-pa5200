# PA-5220 front forwarding and FE100 session commissioning

Physical forwarding and return RX passed on the DAC connection between front
ports 5 and 13. Live FE100 session offload remains blocked by FDT1 calibration
and the subsequent FLU initialization stage. These are separate results.

## Verified packet path

DP boot: `fe8d6e21-6f43-4e7c-b94a-de9c4f75a840`.
Loaded packet module SHA256:
`13bb7f958b44faa2035417250c79caee547ef1f9d0fe5b70b48c05848550eae1`.

The path is Linux TAP → DP raw transport → PKO3 → BGX2/LMAC0 → BCM24
→ front5/13 → physical DAC → opposite front port → BCM24 TM_SSP → PKI/SSO
→ DP raw transport → opposite Linux TAP.

- Created seven missing BCM destination VOQ/connector bundles, including the DP
  trunk and six test front destinations. All eight queue COS attachments and
  ingress/egress connections completed. Allocation refuses an existing VOQ
  topology; it is an explicit commissioning operation, not a general allocator.
- Commissioned BCM24 TM_SSP return metadata and the 5/13 return routes. The
  decoder checks destination24, accepts only selected source system ports,
  strips the four-byte OTMH, and never guesses source identity from Ethernet.
- Fixed duplicate FCS/padding generation: PKO owns these operations; BGX data
  FCS/padding insertion is disabled as required by the local OCTEON SDK.
  The initial double FCS added four bytes to returned payloads, despite working
  wire counters. Exact frame comparison detected this error.
- Small-frame test: both directions passed exact 64-byte payload comparison.
- Full-size raw test: 32/32 1,514-byte Ethernet frames returned on their expected
  opposite port, with 32 PKO completions, 32 wire TX increments and 32 PKI RX.
- TAP integration: all four combinations of direction and frame size (64 and
  1,514 bytes) passed exact frame comparison through the physical DAC.

Evidence: [raw return](FRONT-RETURN-VERIFIED-20260914.json),
[32-frame full-size test](FRONT-RETURN-MTU-20260914.json),
[Linux TAP integration](FRONT-TAP-VERIFIED-20260914.json),
[BCM queues](../../bcm/DP-QUEUES-ALLOCATION-20260914.json).

`validate_trunk_io.py` accepts explicit expected port pairs, sequence counts and
frame sizes. A nonce, complete payload, expected source port and every sequence
must match; TX completion alone cannot pass the test. Example on the DP:

```sh
python3 validate_trunk_io.py --ports 5,13 --pairs 5:13,13:5 \
  --count 16 --size 1514 --rx-format bcm-otmh-ssp --seconds 3
python3 validate_front_taps.py --ports 5,13
```

The TAP test requires down, unaddressed, unbridged test TAPs, temporarily starts
the existing transport/inspection path, and restores their down state afterward.
The updated transport is installed at `/usr/local/sbin/ffn_dp_packet_transport.py`
on the DP. No permanent TAP transport process or production routing configuration
was enabled by the test. The raw DP runtime and BCM commissioning routes remain
available. Results qualify ports5/13 on this boot, not every front port, routing
policy, maximum packet rate, or automatic recovery across an ASIC reset.

## FE100 flow-memory and session implementation

CP boot: `c3b75f79-b285-4a5c-ad61-489d1038a298`.
Reference: the VM's PA-5220 `libpandp_cp.so.1.0`, SHA256
`b57227a460144c8c2545fc2e268b31f475ef72ac2a6f1d457387ab46d842c3e9`.

`ffn_fe100_flow_memory.py` adds per-block clocks, per-channel DDR training and a
bounded FDT1 calibration recovery stage. It uses the verified owner ABI and
board PLL plan, a shared exclusive table lock, one writable register block,
native-call watchdog, live readback, and journals created before any write.
Existing journals prevent blind repeats. TDI reset/training/status is monitored
and protected from these writes.

Both FHM and FDT PLLs locked and clock monitors reached3. FHM0, FHM1 and FDT0
training completed with no register-scope faults. FDT1 returned calibration
error `0x808`; a single post-initialization recovery returned `0x8008`. Its DPHY
and controller still report apparently normal status, demonstrating why those
bits alone cannot qualify the memory. No hardware session entries were inserted.

`ffn_fe100_live_sessions.py` adds a CP endpoint for the existing native IPv4
session adapter. It supplies the audited process-local configuration pointer,
exclusive ownership, bounded native calls and constrained FLU/CFP IA access.
Opening it does not reset or initialize hardware. A writable endpoint requires
successful calibration journals for this boot, live memory status and initialized
FLU/CFP prerequisites. Production packet/offload qualification remains a separate
gate in `NativeSessionAdapter`; the new native session calls have not passed a
live insert/fetch/delete or packet-hit test.

The live status and rejection test ran on the CP. Activation was rejected for
FDT1/FLU prerequisites and its trace contained **zero writes**. Status can be
read with `python3 /usr/local/sbin/ffn_fe100_live_sessions.py` on the CP.

Evidence: [memory attempts](../../fe100/FLOW-MEMORY-20260914.json),
[live session status](../../fe100/LIVE-SESSION-STATUS-20260914.json),
[activation rejection](../../fe100/LIVE-SESSION-GATE-20260914.json).

Remaining hardware work is to resolve FDT1 calibration, verify flow-memory
read/write integrity, initialize and verify FLU tables, then qualify paired
session insert/fetch/delete and actual FE100 packet hits/actions. The direct
BCM/DP forwarding path above does not prove FE100 offload.

## Checks

- Strict MIPS64 big-endian kernel and native adapter builds passed.
- 24 session adapter, journal and live-prerequisite unit tests passed on the VM.
- Four packet envelope tests passed on the actual DP.
- Host C MMIO scope regression passed, including rejection of session access
  to DDR, TDI, unrelated CFP controls, the wrong device and read-only registers.
- Physical packet and live rejection results are linked above.

All hardware code remains within the optional PA5200 provider. No generic FFN
platform dependency was added. Changes and evidence are local; this task did
not commit or push them.
