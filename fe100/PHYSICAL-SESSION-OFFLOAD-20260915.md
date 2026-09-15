# Physical FE100 session actions verified on PA-5220

FE100 forwarded real IPv4 UDP packets using an installed flow's next-hop
override and destination-MAC rewrite. Changing the flow to DROP suppressed
the packets and incremented the hardware drop counter. Deleting the flow
removed the forwarding action. These results include physical ingress on
front port 13, following injection from DP through front port 5 and the DAC.
Egress for this isolated test was the MP capture interface through BCM port 8.

This is a bounded hardware qualification, not general production activation.
Normal front-port routes and the previous parser/LIF configuration were restored.
No production policy offload gate was enabled.

## Reference and fixes

All FE100 register layouts and native calls were checked against the VM's
`/mnt/clones/5220-sysroot1-full`, specifically:

- `opt/dpfs/usr/local/lib64/libpandp_cp.so.1.0`, its DWARF and disassembly;
  SHA256 `b57227a460144c8c2545fc2e268b31f475ef72ac2a6f1d457387ab46d842c3e9`.
- `opt/dpfs/usr/share/pdt/fe100.py` and the corresponding CSR definitions.
- `opt/dpfs/etc/fe-parser.json`, SHA256
  `6dcbd4fa1e12e5798a55bf22dded9f3fdfc5d0ee90d454d8a4ee88238a1bbddf`.

The native FLOWADD operation sends the key and flow ID, but does not install
forwarding actions. The adapter now performs ADD followed by FLOWUPDATE.
SessionManager verifies the final readback and can remove the intermediate
identity entry after an interrupted UPDATE. The supported action encoding
contains CT, next-hop override, optional TTL decrement, and a separate DROP
action. NAT and other unimplemented actions remain rejected. TTL encoding is
unit tested; TTL behavior has not been physically qualified in this run.

Initially FLOWUPDATE returned success while CFP reported FCM allocation
failure. Both FCM DDR channels were still in reset and SEM had no free counter
IDs. New staged wrappers initialize FCM clocks, train channels 0 and 1, then
initialize SEM counter memory and its free lists. The native sequence comes
from `pan_fe100_set_sem_fcm_config_thread`, with writes confined to each
stage's register block. Both training stages completed without scope faults;
SEM supplied 112 free counter IDs to the FLU FIFO after initialization.
The four stage journals are retained alongside this document with the CP boot
ID in their filenames. These results do not establish memory capacity,
temperature margin, or long-duration reliability.

Action writes now require successful current-boot FCM/SEM journals, live
clock/controller readiness, SEM initialization, and an available counter ID.
CPU identity-table operations retain their independent FHM/FDT/FLU checks.
Native completion is still followed by action readback: a completion response
alone did not prove allocation or installation on this hardware.

FLOWUPDATE readback can contain nonzero inactive NAT payload with NAT flags
clear. The decoder now checks the NAT action flags, preserves supported action
state, and does not mistake unused NAT bytes for an enabled NAT action.

BCM required dedicated queue bundles for FE100 port 3 and MP port 8. Both
bundles were allocated and connected across all eight queues without replacing
the existing bundles. The allocation command refuses to run if either bundle
already exists; do not replay the old full allocation recipe.

## Packet evidence

Path:

`DP ffnpkt0 → PKO3/BGX → BCM24 → front5 → DAC → front13 → BCM7 → FE1003 → TMI → BCM20 → MP8`

The MP software fabric service was stopped. The MP only captured packets;
it did not perform the rewrite or relay traffic. The test installed an exact
UDP tuple in zone 4094 and next-hop 31 with destination MAC
`02:52:20:ab:cd:ee`. Its baseline next-hop slot 30 was empty, so normal LIF
forwarding could not explain the observed rewritten packets. Captured bytes
matched the nonce-bearing Ethernet frame exactly except for the intended MAC
rewrite, following the 32-byte FE100 CPU header.

| Four-packet phase | Captured original | Captured rewritten | ASIC evidence |
| --- | ---: | ---: | --- |
| Parser bypass baseline | 4 | 0 | Physical path reaches MP capture |
| No installed action | 0 | 0 | 1 miss, then 3 learned identity hits |
| Installed forwarding action | 0 | 4 | 4 flow hits, 4 CT actions, 4 direct next-hop lookups, 4 SEM increments |
| Installed DROP action | 0 | 0 | 4 flow hits and 4 hardware drops |
| Session removed | 0 | 0 | 1 miss, then 3 learned identity hits; no rewrite |

An earlier one-packet run also passed all five phases. The repeated test
requires both packet evidence and hardware counter deltas, rejects duplicate
captures, and requires confirmed cleanup. Counter snapshots include background
traffic; the dedicated flow-action counters agree with the injected count.

Evidence files:

- `PHYSICAL-SESSION-SINGLE-20260915.json`: first passing physical run.
- `PHYSICAL-SESSION-REPEAT-20260915.json`: four-packet run with counter qualification.
- `PHYSICAL-SESSION-JOURNAL-20260915.json`: CP write intents, readbacks,
  snapshots and successful restoration of all temporary table changes.
- `PHYSICAL-SESSION-READINESS-20260915.json`: no table or action prerequisites
  blocking the initialized hardware. The global production qualification flag
  remains false intentionally.
- `../bcm/FE100-SESSION-QUEUES-20260915.json`: queue allocation responses.
- `PHYSICAL-SESSION-FRONT-RESTORED-20260915.json`: after cleanup, all 16
  ordinary 1514-byte packets returned correctly across front5/front13 through
  DP, with 16 PKO completions and 16 wire receives.

## Controls and validation

`ffn_fe100_packet_lab.py --serve` is the CP commissioning controller. It
supports prepare, install, drop, remove, snapshot and finish commands. It holds
an exclusive table lock, restricts the test's table indices and tuple, checks
the pinned owner and parser hashes, journals intent before writes, verifies
readback, and restores owned entries on finish, EOF or command timeout.
Native workers have bounded execution. A restore failure remains recorded as
requiring recovery; it is never reported as success.

On the currently commissioned MP, run:

```sh
python3 /usr/local/sbin/validate_physical_sessions.py --count 4
```

This changes BCM7's route temporarily, enables MP capture, programs isolated
FE100 test entries, and restores the prior configuration. It requires the
existing front5/front13 DAC loop, initialized hardware and the deployed CP/DP
helpers. It is not a general traffic generator or an unattended boot service.

39 targeted tests pass on the VM:

```sh
python3 -m unittest test_packet_sessions test_sessions test_session_adapter \
  test_session_journal test_live_sessions test_physical_sessions
```

Coverage includes action/native encoding, ADD/UPDATE ordering, interrupted
update recovery, unsupported state rejection, stale or failed readiness
journals, probe checksums, missing counter evidence, duplicate packets,
unexpected drop-phase packets and stale forwarding after removal.

Remaining production work includes policy-driven session admission and
invalidation, front-port egress and bidirectional session qualification,
TCP/IPv6/NAT coverage, routing TTL/checksum behavior, full-frame and load tests,
aging/counter export, and restart recovery qualification. The commissioning
wrappers require the referenced vendor owner library on the appliance; they
are not a complete replacement FE100 driver.
