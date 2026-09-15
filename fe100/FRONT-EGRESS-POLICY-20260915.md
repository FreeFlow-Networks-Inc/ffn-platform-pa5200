# FE100 front egress and policy integration — 2026-09-15

Only the user's front 5–13 DAC pair was used for packet validation. No production
offload admission has been enabled. The tests use isolated IPv4 UDP benchmark
tuples, zone 4094, nonce-tagged frames, and reserved commissioning table entries.
The distinct-port capture uses VLAN 4000 and return zone 4093.

## Verified physical egress

| FE100 ingress / egress | Physical return to DP | Rewritten frames | Drop / removal | Evidence |
| --- | --- | --- | --- | --- |
| 13 / 13 | front 5 over DAC | 1/1 | no returned frames; hardware counters agree | `FRONT13-SESSION-EGRESS-20260915.json` |
| 5 / 5 | front 13 over DAC | 4/4 | no returned frames; hardware counters agree | `FRONT5-SESSION-EGRESS-20260915.json` |

Both runs verified PKO completion, exact Ethernet bytes after destination-MAC
rewrite, OTMH source-port identity, FE100 session-hit/cut-through/SEM counters,
drop counters, and misses after removal. Neither run uses a software forwarding
process. Their FE100 and BCM changes were restored with no reported errors.

These are two separate same-port FE100 forwarding tests. They prove that both
front ports can carry FE100 egress and that the DAC return reaches DP RX. They
do **not** establish forwarding between distinct ingress/egress ports,
simultaneous bidirectional flow handling, TCP, IPv6, routing/TTL processing,
throughput, session aging, or production policy enforcement.

The later VLAN return tests verified **both distinct-port directions** with
destination-MAC/VLAN rewrite, TTL decrement, correct IPv4 checksum and exact
physical return capture on MP:

| FE100 ingress → egress | Rewritten frames | Drop / removal | Evidence |
| --- | --- | --- | --- |
| 13 → 5 | 1/1 | passed; cleanup restored | `FRONT13-TO-5-VLAN-SESSION-20260915.json` |
| 5 → 13 | 4/4 | passed; cleanup restored | `FRONT5-TO-13-VLAN-SESSION-20260915.json` |

Each frame crosses the DAC twice. The tests ran sequentially, not with a live
forward/reverse policy pair installed concurrently. The consolidated evidence is
`BIDIRECTIONAL-SESSION-VALIDATION-20260915.json`. It confirms the same CP boot,
restored FE100 journals, baseline BCM routes, removed test group and MP rx-all
restoration. General routing, concurrent paired-session policy admission and
production traffic remain unqualified.

## Reference-derived implementation

The VM reference is `/mnt/clones/5220-sysroot1-full` (the earlier user path used
`/mnt/clone`; the accessible VM mount is `/mnt/clones`). Relevant files:

- `opt/dpfs/usr/local/lib64/libpandp_cp.so.1.0`, DWARF and native table APIs;
  SHA256 `b57227a460144c8c2545fc2e268b31f475ef72ac2a6f1d457387ab46d842c3e9`.
- `opt/dpfs/usr/share/pdt/fe100.py` and
  `opt/dpfs/usr/lib/python2.7/site-packages/pcs/packets/fe100.py`.
- `opt/dpfs/etc/fe-parser.json`, SHA256
  `6dcbd4fa1e12e5798a55bf22dded9f3fdfc5d0ee90d454d8a4ee88238a1bbddf`.
- VM OpenBCM reference `openbcm/sdk-6.5.26-DNX.1`, particularly
  `src/soc/dpp/ARAD/arad_ports.c`, `src/bcm/dpp/port.c`, and `field.c`.

Normal front forwarding uses a DIRECT next hop with `do_sysport=False`, an
egress LIF referencing LEF31, and a TX port map (logical 13 → BCM7; logical 5 →
BCM16). The CPU `do_sysport=True` four-bit destination is not a general front-port
number limit.

LEF is a 10-byte native structure in block `0x58000`. TX port-map entries are
three bytes in NIF block `0x10000`. IPv4 QMAP uses a 36-byte view of an 84-byte
union in block `0x80000`. Its native fetch selector `pt=1` is not preserved by
hardware; comparison excludes that selector, generated validity bits and ECC,
while retaining the programmed match/action fields.

Without express forwarding, physical egress still carried the 24-byte FE100 CPU
message header. QMAP `XF=1` removes that header. On this appliance XF output
uses NIF with an eight-byte DSA tag after the MAC addresses. A BCM rule matching
NIF ingress, the synthetic destination MAC and exact destination-port DSA bits directs
it to the front queue; the existing RAW_DSA egress handling removes the tag.
VLAN and tagged/untagged bits are masked to support the VLAN return test.
Neither the FEDIRECT bit alone nor successful queue submission is proof of a
valid Ethernet frame on the wire.

## Distinct-port commissioning

`validate_front_sessions.py --cross` and `--cross --front5` configure separate
ingress and egress ports. They require BCM rules to send the original packet
to FE100 and capture the rewritten return at DP after its second DAC crossing.
This avoids treating a recirculating loop as successful forwarding.

The original BCM RAW-ingress capture method did not qualify forwarding. Initial
attempts either failed BCM rule setup or saw no packets reach the FE100 flow
pipeline. One SDK allocation collided with an already allocated TCAM database;
the test leaves that database untouched and preflights explicit lab group IDs.
All evidence records distinguish this failure from successful physical tests.
BCM RAW-port PMF integration remains unresolved.

The final trap/control-flags attempt also received zero FE100 flow hits or
misses. Its temporary rule/trap removals and FE100 table readback restoration
completed without reported errors. See `DISTINCT-PORT-ATTEMPT-20260915.json`;
the allocation-conflict output is preserved separately in
`BCM-INGRESS-ALLOCATION-CONFLICT-20260915.json`.

The successful alternative is `--cross --vlan-return`. Native LIF matching
separates untagged ingress (VID 0) from the physical return (VID 4000). The
native packed 80-bit key places VID at bits49:38 and pport at bits37:32, verified
against DWARF member byte offsets and bit offsets. LIF31 sends only the matching
tagged return to MP capture through SYSPORT8. The reserved return flow key is
preflighted absent and included in cleanup ownership in case hardware learns it.

DP injects the untagged frame on the other end of the DAC. FE100 processes its
first physical ingress with the installed session, rewrites DMAC, adds VLAN4000,
decrements TTL, and transmits on the distinct egress port. The frame crosses the
DAC again, matches the tagged-return LIF, and is captured on MP. Validation
requires the exact Ethernet frame, correct CMH ingress-port metadata, hardware
session/SEM counter deltas, and no returned frame after drop or removal. TTL
decrement also bounds recirculation if VLAN separation fails. MP promiscuous
capture and rx-all settings are temporary and restored after the test.

## Optional policy control

`ffn_fe100_policy.py` consumes decisions from a trusted FFN policy evaluator and
owns a forward/reverse session pair. Admission requires an applied policy
generation, current verified port bindings, an established allowed flow, and
no NAT or inspection requirement. It rejects stale decisions. Replacement,
revocation, binding changes observed by the owner, and recovery remove both
directions. Partial installation is rolled back and uncertain removal prevents
further admission.

This is an owner interface, not a new firewall evaluator. Production admission,
continuous link/policy event subscription, aging, NAT, inspection handoff, and
automatic reactivation are not connected or qualified. The deployed CP CLI
exposes **status and drain only**, with admission permanently unqualified.

Core FFN has an optional `platform_policy_guard` callback, loaded only by an
explicitly selected extension declaring `policy_barrier_version: 1`. The actual
configuration-commit handler awaits this guard before writing the running
configuration. Drain failure returns HTTP 409 and releases the configuration
lock. An absent extension makes no hardware call. A declared but unavailable
barrier fails closed.

The PA-5200 guard hashes the candidate bytes, reads the CP owner revision and
drains its SQLite-journaled sessions. Debian SQLite is explicitly preloaded to
avoid the incompatible library in the vendor search path. The current guard
stays blocked after a commit. Partial commits also conservatively drain all
owned sessions; the candidate hash is not an acknowledgement that the resulting
running configuration has been applied.

The hook is installed on the MP in `/opt/ffn-ngfw-v2` and the selected extension
`/opt/ffn-platforms/pa5200-management`, preserving the newer deployed controllers.
Backups: `/var/backups/ffn/policy-barrier-1789486966875729824/manifest.json`.
An actual MP → CP empty-journal drain and selected-extension callback passed.
The management service restarted successfully and its API responds with the
expected HTTP 401 to an unauthenticated request. No real policy configuration
was committed as a test. Direct controller mutations and non-API configuration
writers still need the same invalidation integration before production admission.

## Automated checks

44 FE100 encoding/session/journal/policy tests passed on the VM. Eleven core
extension, actual commit-handler ordering, and MP guard tests passed in an
isolated test directory on the MP. These include reverse UDP/IP checksums,
QMAP readback ownership, stale/unsupported policy rejection, pair rollback,
failed drain, and generic-platform isolation. No production Python packages
were changed for these tests.
