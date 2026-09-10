# PA-5220 commissioning fabric, 2026-09-09

Physical L2 and IPv4 L3 forwarding passed on the appliance. This is a
four-port software prototype through an authenticated MP-to-DP SSH stream,
with MTU 1500. It is not ASIC forwarding offload or a line-rate claim.

The live path is front port -> BCM3/NIF -> FE100 -> TMI/BCM20 -> MP BCM8
NIC -> DP TAP. DP egress travels over the same SSH stream to MP BCM9,
using direct-system-port ITMH plus eight bytes of RAW_DSA padding.
The FE100 32-byte CMH supplies ingress port and original Ethernet length.

| Front | BCM destination | FE logical port | LIF index |
|---|---|---|---|
| 1 | 28 | 1 | 3 |
| 3 | 14 | 3 | 4 |
| 5 | 16 | 5 | 2 |
| 13 | 7 | 13 | 1 |

LIF entries send to system port 8; BCM front ports above force-forward to
BCM3, and BCM20 force-forwards to BCM8. Destination queues must already
exist. The allocation modes in `prepare-forward-test.py` are one-time
operations per BCM initialization: do not repeatedly allocate queues.

## Verified hardware tests

- ARP request entered front13, reached DP p13, and returned through front13.
- L3: 100/100 frames routed p13 to p5, through physical front ports and
  FE100, with TTL 64 -> 63, correct MAC rewrite and IPv4 checksum.
- L2: 100/100 frames traversed front3 -> cable -> front1 -> DP VLAN100
  bridge -> front5 -> cable -> front13. No missing, duplicate or corrupt
  returned frames. The test disabled p3 and temporarily changed p5 to L2
  to prevent a loop through the physical test cables; both were restored.
- Codec tests pass on both MP x86 and DP MIPS64-BE, including fragmented
  stream input, invalid headers, maximum frame size and padding removal.

Evidence is in `FABRIC-L2-VALIDATION-20260909.txt` and
`FABRIC-L3-VALIDATION-20260909.txt`. `test-fabric-*.py` require the documented
test topology and matching port configuration, and are not general probes.

The expanded `test-fabric-matrix.py` passed physical access and tagged VLAN200
forwarding, MAC learning, VLAN300 isolation, disabled-egress enforcement,
runtime L2→L3 transitions, IPv4/IPv6 routing and TTL/hop-limit-one rejection.
It varies frame sizes through MTU 1500 and compares complete returned frames,
including rewritten MAC addresses, decremented hops and checksums. All 1,200
expected forwards arrived intact; 660 expected drops had confirmed physical
ingress and no observed egress. Original settings were restored. Evidence:
`FABRIC-MATRIX-VALIDATION-20260909.jsonl`.

The DP ingress path now also supports runtime packet inspection through the
existing C analysis engine. See `INLINE-ANALYSIS.md` for controls and limitations.

## Runtime

On the MP, `systemctl start/stop ffn-fabric.service` attaches/detaches the
relay. `ffn-network status` reports attachment and `ffn-network patch`
continues to change individual ports at runtime. Disabled ports drop frames
without terminating the relay. Attached ports reject MTUs above 1500.

The relay temporarily enables MP internal NIC enp8s0f0, enables rx-all on
enp8s0f1 and raises their MTUs for transport headers. Normal exit restores
the previous state. The MP management NIC is unaffected.

The service is deliberately not enabled at boot: BCM queue allocation and
FE table replay are not yet integrated into a reproducible fabric startup.
Ports 23/24 have verified links but are not attached to this relay. Complete
all-port startup, link propagation, overload handling, recovery after BCM
restart and sustained load testing before treating this as a deployed fabric.

## Basic connectivity and relay recovery, 2026-09-10

Added `--restart` to `test-fabric-matrix.py`. The physical matrix passed
1,800 expected forwards and 660 expected blocks. This includes one MP relay
restart in L2 mode and another in L3 mode, with 300 intact forwarded packets
after each. Port configuration, interface indices and MAC addresses survived
both restarts. The test waits for transport NIC carrier and bridge convergence
before validating delivery. This establishes relay restart recovery only;
it does not establish full appliance reboot or BCM/FE initialization recovery.

Added `test-fabric-host.py` for basic host connectivity through front13 using
the physical 5--13 cable. ARP and IPv6 neighbor advertisements passed, followed
by 20 IPv4 and 20 IPv6 echo replies with verified payloads and checksums.
The test uses dynamic neighbor discovery, rejects preexisting test neighbors,
and removes only neighbor entries associated with its test MAC afterward.
It installs no permanent neighbors and leaves port settings unchanged.

Original port settings were restored at revision38: p1/p3 are VLAN100 access
ports; p5 and p13 retain their respective 198.18.1.0/24 and 198.18.2.0/24
subnets and IPv6 addresses. Fabric and both thermal services remain active.
Evidence: `BASIC-CONNECTIVITY-20260910.jsonl` and
`HOST-CONNECTIVITY-20260910.jsonl`. These remain paced functional tests,
not throughput or all-port qualification.
