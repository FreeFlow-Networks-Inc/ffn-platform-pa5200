# PA-5220 SDK integration, 2026-09-09

This implementation uses the SDK and owner driver on the build VM. It does
not yet provide a complete OCTEON/FE100/BCM hardware datapath or an OVS hardware
offload provider. Successful SDK calls alone are not proof of packet offload.

Follow-up: [FE100 parser diagnosis and recovery](../../fe100/PARSER-OFFLOAD-PROGRESS-20260909.md)
identifies zeroed parser entries, implements verified encoding, and records
the downstream lookup stall. The working SYSPORT configuration was restored.

| Component | Implemented and tested | Remaining limitation |
|---|---|---|
| OCTEON III COP2 | Linux Crypto API AES adapter using local SDK instructions; AES-128/192/256 encrypt/decrypt known-answer tests; MACsec traffic and wrong-key rejection | GCM uses generic GHASH; no throughput gain measured |
| OCTEON PKI/SSO/PKO3 | Existing SDK executive backend and build recipe retained | Not activated alongside Linux: resource ownership, DMA and queue coexistence remain unfinished |
| FE100 | Hash-pinned owner-library DIRECT next-hop access; MAC/VLAN rewrite fields read back; temporary entries removed | DIRECT LIF packet test delivered 0/300; original SYSPORT path restored. No new rewrite/route offload claim |
| BCM | Exact ingress-port/EtherType/source-MAC PMF rule create/delete through live SDK | Redirect packet tests failed, including with ingress force-forward released. No counter processor configured; experimental rules removed |
| OVS/OVN | MIPS64-BE userspace, kernel datapath, physical forwarding tests | TC `skip_sw` rejected on TAP (`Operation not supported`); no hardware flow provider |

## OCTEON AES

`../kctl/ffn_octeon_aes.c` registers `aes-ffn-octeon`, priority 300.
`build-sdk-aes.sh` extracts seven AES instruction macros from the owner's
`/mnt/clones/sdk51/OCTEON-SDK/executive/cvmx-asm.h` into the build directory.
No SDK header or owner binary is added to this repository. The source header
SHA-256 was `2f011a820b0dc1ddb7e6c913eb50537a473ccd70de987fffa014a4ce7e5b9da0`.

The adapter uses Linux `octeon_crypto_enable/disable` for COP2 task state and
preemption. It does not enable userspace raw physical memory access or invoke
SDK global packet-I/O initialization. Module registration follows all six
AES known-answer checks. The tested module hash is
`1690bee4b3388267229b4c9d5f69e5638fd8787c0b3b473a8d9cc0aa57c4c3ed`.

Installed DP module: `/usr/local/lib/ffn_octeon_aes.ko`.
`ffn-octeon-crypto.service` is enabled and active; startup rejects incompatible
modules through the kernel loader and verifies driver registration. This
boot unit was started on the live DP, but a second reboot has not been tested.
Rebuild the module whenever changing its kernel. Previously allocated crypto
transforms retain their selected driver; restart their consumers to select a
new implementation. Do not force-unload an in-use crypto module.

The live `/proc/crypto` lists `ctr(aes-ffn-octeon)` and
`gcm_base(ctr(aes-ffn-octeon),ghash-generic)`. MACsec uses the hardware AES
primitive, not a BCM PHY MACsec engine. Keys in commissioning tests are
ephemeral and are not saved in configuration or evidence.

## FE100 owner ABI

Reference root: `/mnt/clones/5220-sysroot1-full/opt/dpfs` on the VM.
`usr/share/pdt/fe100.py:1580` defines next-hop rewriting, and its LIF diagnostic
defines forwarding type 4 as DIRECT and 5 as SYSPORT. Runtime library is
hash-pinned as documented in `../../fe100/DRIVER-REFERENCE-20260909.md`.
DWARF confirms `pan_fe100_nexthop_entry_t` is 16 bytes, big-endian:
valid bit23, sysport bit20, VLAN-enable bit18, destination-MAC-enable bit16;
egress-LIF offset4, VLAN6, MTU8 and destination MAC10. The owner's
`fe100_ia_cmd_addr(10)` returned `0x50300`; access is confined to FWD block
`0x50000..0x57fff` and the audited register allowlist.

`ffn_fe100_nexthop.py` defaults to inspecting slot31. `--roundtrip` refuses a
valid slot, programs a test entry, verifies payload and removes it in finally.
`--direct-lif-test` is an explicitly experimental commissioning command. It
temporarily replaces only verified lab LIF3 (front1) for 20 seconds, restores
its captured entry with readback, then removes next-hop31. The two processes
share the table lock; separate MMIO adapters limit FWD and TLU register access.
This test failed to forward and is not enabled as a persistent service.

Evidence: `../../fe100/NEXTHOP-VALIDATION-20260909.jsonl` and
`../../fe100/DIRECT-PACKET-ATTEMPT-20260909.txt`. The latter intentionally
includes the failed packet assertion and successful restoration.

## BCM field backend

VM OpenBCM reference: `/mnt/clones/openbcm/sdk-6.5.26-DNX.1`.
`src/bcm/dpp/port.c:bcm_petra_port_force_forward_set` delegates to ingress
trap configuration. The exact commissioning rule matches front1/BCM28,
EtherType `0x88b5`, source `02:52:20:ab:cd:91`, requesting redirect to front5's
system port16. Group/entry IDs are allocated, returned and explicitly supplied
to cleanup. A missing PMF counter processor is reported as unavailable.

With the baseline trap enabled, all 300 packets arrived on the original
front1 path. With that trap disabled, none reached the expected front13 capture.
The rule was removed and the trap restored; both 300-packet control probes
then passed on the original path. This leaves PMF matching, action resolution
and ingress pipeline prerequisites to investigate. No broad BCM reset was run.

The owner config sets front-port ingress to RAW. A second SDK recipe uses
packet-start data qualifiers at offsets6 and10 to match the same eight source
MAC/EtherType bytes without parsed Ethernet metadata. It created successfully
(group0, entry0, data qualifiers0/1), but reproduced both failures. All four
objects were destroyed and the original trap restored. RAW framing alone is
therefore not an established explanation. Raw logs are
`../../bcm/PMF-RAW-TRAP-20260909.txt` and
`../../bcm/PMF-RAW-NO-TRAP-20260909.txt`.

The subsequent physical L2/L3 matrix passed 1,200 expected forwards and 660
expected blocks, with original port configuration restored. Final service
and crypto registration evidence is `SDK-FINAL-STATE-20260909.json`;
the matrix is `VIRTUAL-FABRIC-MATRIX-20260909.jsonl` (run before the final RAW
PMF attempt, followed by another restored-path control probe).

An additional RAW-data rule with the SDK's explicit traffic-management
preselector also installed successfully but failed the same two physical
tests. Its group, entry, two data qualifiers and preselector were destroyed.
See `../../bcm/PMF-TM-TRAP-20260909.txt` and
`../../bcm/PMF-TM-NO-TRAP-20260909.txt`.

## Next development requirements

1. Resolve FE100 DIRECT forwarding and BCM PMF steering with packet evidence
   before exposing them as enabled offload capabilities.
2. Establish explicit PKI/SSO/PKO3 resource ownership and Linux coexistence
   before using the SDK executive's global initializer on the live DP.
3. Add a hardware flow provider with supported match/action reporting,
   resource accounting, statistics and rollback. An OVS `hw-offload` setting
   alone cannot supply one; see the [OVS TC offload documentation](https://docs.openvswitch.org/en/latest/howto/tc-offload/).
4. Benchmark acceleration, add hardware GHASH/batched crypto where supported,
   and implement managed MACsec key exchange separately from interface setup.
