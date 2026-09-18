# PA-5220 packet runtime and session adapter — 2026-09-14

Later physical return-path and TAP validation, the double-FCS fix, and current
FE100 blockers are recorded in [the front-forwarding report](FRONT-FORWARDING-20260914.md).
The measurements below describe the earlier runtime commissioning stage.

## Implemented

`../kctl/ffn_dp_trunk.h`, included by the packet initializer, implements a
single-queue CN78XX packet runtime on the internal BGX2/LMAC0 40G link. It
registers the raw `ffnpkt0` netdev after the explicit initialization stages.
PKI style/QPG/PKND8 use the verified packet aura; SSO group0 supplies work;
PKO3 DQ0 uses the L1–L5 hierarchy, MAC12/FIFO0, and BGX channel0xa00.
The channel backpressure lookup is explicitly mapped at compressed index0x280.
Without that mapping, DMA completions occurred without wire transmissions.

TX submits real PKO commands and keeps each coherent buffer until a separate
hardware completion write. RX validates WQE and packet-chain addresses against
the driver's allocations before copying or freeing. The raw interface accepts
the commissioned ITMH/RAW_DSA envelope, suppresses IPv6 address configuration,
and is intended for the existing `ffn_dp_packet_transport.py` AF_PACKET adapter.
It is not an ordinary IP interface. The completion worker cannot wake TX during
shutdown; RX and outstanding completions can continue draining.

FPA aura QOS drops are disabled for the dedicated pools while hard allocation
limits remain enforced. Readback excludes the dynamic occupancy byte rather
than requiring a moving hardware counter to equal the programmed value.
Packet, SSO and PKO allocations stay pinned whenever hardware may reach them.
A partial initialization or DMA fault requires a DP reset, not module unload.

`ffn_dp_packet_init.py` and the MP `ffn-dp-prepare` bridge expose serialized
`prepare-trunk`, `start-trunk`, and `stop-trunk` controls. The optional PA5200
management backend and harness recognize those actions. No generic FFN core
hardware dependency or automatic packet-engine startup was added.

`../../fe100/ffn_fe100_session_adapter.py` implements the boundary between the
64-byte FFN session entry and the native aligned 144-byte owner union. It
supports the audited IPv4 identity entries, validates returned keys and state,
and fetches the actual flow ID before delete. Errors and ambiguous writes
propagate to the existing journal/reconciliation manager. Its ctypes endpoint
must be embedded in an already initialized, exclusively locked, bounded owner
service. Loading the shared library alone does not initialize its device
pointers. Production insertion remains gated on physical qualification.

`../dpfwd/ffn_dp_io_octeon.c` now has an explicit synchronous copy callback for
offload handoff. Missing or rejecting adapters are counted separately from
accepted copies. WQE ownership is released once, after the callback, and a
failed mandatory-inspection handoff does not silently bypass inspection.

## Physical evidence

The first tested module had SHA256
`9d21206c662785ee57b62779cfe8d353e812c4a34574f13a3ce0c1a56e67799f`.
On DP boot `3639d48d-a90e-4f01-948c-2bcd3be8c75c`, two six-packet bursts,
separated by stop/start, produced:

| Observation | First burst | After restart and second burst |
|---|---:|---:|
| PKO accepted | 6 | 12 |
| DMA completed | 6 | 12 |
| BGX wire TX | 6 | 12 |
| BCM internal port24 RX without errors | 6 | 12 |
| BGX wire RX / matched return frames | 0 / 0 | 0 / 0 |
| Packet buffers available | 512 | 512 |
| DMA faults | 0 | 0 |

The evidence files `PACKET-RUNTIME-FIRST-BURST-20260914.json` and
`PACKET-RUNTIME-REPEAT-BURST-20260914.json` contain before/after counters and the
boot ID. Queue-buffer availability changes asynchronously as PKO opens/closes
and returns cached storage; it is distinct from packet-buffer ownership.

The final shutdown-race correction has module SHA256
`f421216576abf1f9276336b2644b63e78e165d9b3dd42b6b1aaa8bdaeef029cf`.
On boot `13c0c8ed-1cc6-44cb-a0b7-88f813415ccd`, it repeated the same two bursts
with stop/start between them: 12 accepted, 12 completed, 12 BGX wire TX,
zero wire RX, zero DMA faults, and all 512 packet buffers available afterward.
Independent BCM port24 counters increased from 12 to 24. See the `FINAL-BURST`,
`FINAL-REPEAT` and `FINAL-STOP` JSON files in this directory and
`../../bcm/PACKET-RUNTIME-BCM-{BEFORE,AFTER}-20260914.json`.

Final state: `ffnpkt0` stopped, DQ closed, PKI and PKO disabled, allocations
still pinned, no failed Debian systemd services. CP/MP were not restarted.
FE100 external clocks remained ready; no packet/lookup counter deltas were
observed. Two TCAM response FIFO status fields remained at 5 in both samples;
that observation does not qualify session lookup or offload.

## Build and software validation

- Both packet initializer and read-only CSR probe compile for MIPS64 big endian
  against `6.18.49-ffn-debian-dp-20260914+` with `KCFLAGS=-Werror`.
- VM Python packet control, session/journal/adapter and transport suites: 35 tests.
- Optional management harness suite: 14 tests, including runtime transitions,
  stale boot IDs, faulted shutdown and incomplete postconditions.
- Optional runtime API suite: 4 tests on the MP, covering administrator checks,
  audit records, boot-ID payload validation and sanitized errors without retry.
- Native DP Python run: 29 tests, including the new native-layout adapter tests.
- C `make oct-test oct3-test`: both suites passed, including accepted copy,
  rejected handoff, unavailable adapter and packet ownership cases.

The software suites use simulated hardware endpoints. They validate control
and ownership behavior; they do not establish FE100 offload or packet RX.
Vendor references were read locally from `/mnt/clones/sdk51/OCTEON-SDK` and
`/mnt/clones/5220-sysroot1-full`. The FE100 adapter pins owner SHA256
`b57227a460144c8c2545fc2e268b31f475ef72ac2a6f1d457387ab46d842c3e9`.

The updated management modules are staged in the isolated MP harness at
`/opt/ffn-platforms/pa5200-hardware`. The optional router still requires an
explicit production WebUI mount with FFN authentication and audit callbacks.

## Explicit commissioning controls

After loading the commissioning module and confirming Debian boot health and
the internal link, execute these stages on the DP (or via the MP bridge):

```sh
python3 /usr/local/sbin/ffn_dp_packet_init.py prepare-pki
python3 /usr/local/sbin/ffn_dp_packet_init.py prepare-dma
python3 /usr/local/sbin/ffn_dp_packet_init.py prepare-sso
python3 /usr/local/sbin/ffn_dp_packet_init.py prepare-pko-memory
python3 /usr/local/sbin/ffn_dp_packet_init.py prepare-pko-queues
python3 /usr/local/sbin/ffn_dp_packet_init.py prepare-trunk
python3 /usr/local/sbin/ffn_dp_packet_init.py start-trunk
python3 /usr/local/sbin/ffn_dp_packet_init.py stop-trunk
```

The final two commands are separate operator actions. Other SDK allocators or
packet owners must not claim the same hardware concurrently. The read-only
`ffn_dp_packet_probe.ko` provides fixed CSR diagnostics without arbitrary writes.
`validate_trunk_io.py --ports <explicit-test-ports>` sends nonce-tagged frames
and reports DMA, wire and returned-frame evidence separately; exit2 means a
round trip was not established.

## Qualification remaining

DP-to-BCM transmission is physically demonstrated. Front-port egress,
bidirectional forwarding, PKI/SSO packet receipt, sustained traffic and FE100
session offload are **not yet physically verified**. Current tests receive no
return frames, so `ready` and `physical_forwarding_verified` remain false.
BCM header/queue steering and the return path need commissioning. The FE100
adapter needs a qualified initialized owner endpoint and live insert, lookup,
hit/action, delete and recovery tests. No FE100 production session was installed.

This is a single-queue commissioning runtime, not a production line-rate
dataplane. It polls SSO, copies packets, and caps raw TX at 3584 bytes. Earlier
dated notes in this directory are historical snapshots; the evidence above
supersedes their statements that DQ opening and wire transmission are absent.
