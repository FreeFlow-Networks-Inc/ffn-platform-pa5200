# PA-5220 autonegotiation connectivity validation — 2026-09-09

Tested the installed cables connecting front ports 5–13 and 23–24 on the live appliance. BCM SDK autonegotiation status was enabled, links were up, and remote advertised abilities were readable before traffic tests.

| Cable pair | Negotiated speed | Packets each direction | Missing / duplicate / corrupt |
| --- | --- | --- | --- |
| SFP+ 5 ↔ 13 | 10 Gb/s | 3,000 | 0 / 0 / 0 |
| QSFP 23 ↔ 24, advertisements restricted to 40G | 40 Gb/s | 3,000 | 0 / 0 / 0 |
| QSFP28 23 ↔ 24, original advertisements | 100 Gb/s | 3,000 | 0 / 0 / 0 |

All 18,000 IPv4/UDP test packets passed sequence and payload validation. Each direction used three bursts of 1,000 packets with approximately 0.5 ms pacing. This validates connectivity at the negotiated PHY speed; it is not a line-rate throughput benchmark. Results cover these installed cables and ports, not every transceiver or unconnected cage.

## Paths and evidence

- SFP+: MP ITMH injection → BCM destination 16 or 7 → physical cable → peer BCM port → FE100 → MP capture. Validated payload offset 74 includes the 32-byte FE100 header.
- QSFP: MP ITMH injection → BCM destination 34 or 35 → physical cable → peer BCM port → temporary direct BCM return to MP. Payload offset 42. This test does not establish QSFP integration into the FE100/DP forwarding fabric.
- `ffn-an-sfp-*.log`: 10G packet results.
- `ffn-an-qsfp-*.log`: 100G packet results.
- `ffn-an-qsfp40-*.log`: 40G packet results.
- `autoneg-before.json`: original forced-speed configuration.
- `autoneg-after.json`: settled 10G and 100G autonegotiated links.
- `autoneg-40g.json`: settled 40G autonegotiated links.
- `autoneg-final.json`: final link, advertisement, peer ability, and forwarding state.

## Final live state and cleanup

Autonegotiation remains enabled on all four tested ports. Ports 5 and 13 are up at 10G; ports 23 and 24 are up at 100G. QSFP advertisement speed mask was restored to its captured original value `0x5000008` after the 40G-only test. Temporary force-forward rules on BCM 34 and 35 were disabled and read back as destination 0, enabled 0. Existing SFP forwarding to BCM 3 was preserved. MP fabric, MP thermal telemetry, and CP thermal governor services were confirmed active.

The boot front-port initializer still uses its prior forced-speed policy. These AN changes are live runtime settings; this test did not make them persistent across BCM initialization or reboot.

Two queue groups were allocated once for the test and remain allocated, without temporary ingress return rules:

| Front port | BCM port | Core | Connector | VOQ | System port |
| --- | --- | --- | --- | --- | --- |
| 23 | 34 | 1 | `0xc4080028` | `0x243c0034` | `0x6c000022` |
| 24 | 35 | 1 | `0xc4080030` | `0x243c003c` | `0x6c000023` |

Do not repeat allocation modes 23/24 during this BCM process lifetime. The lab credit configuration is sufficient for this paced connectivity test, not validated for line-rate operation.

The CINT recipe and renderer now provide AN inspection/enabling, temporary QSFP test forwarding, 40G-only advertising, and restoration of the captured advertisement mask. Mode 28 is specific to this captured PA-5220 configuration. The VM's local OpenBCM headers were used for SDK definitions.
