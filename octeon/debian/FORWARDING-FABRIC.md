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
