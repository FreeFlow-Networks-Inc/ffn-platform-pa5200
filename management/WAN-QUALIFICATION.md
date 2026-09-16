# WAN1 packet-path qualification

`wan-path` is an optional PA5200 resource in the MP journal worker, reached
through `ffn-controld`. It tests copper panel port 1 / PHY17 / BCM28 using
the commissioned BCM24 TM_SSP trunk and the OCTEON `ffnpkt0` packet driver.
It does not configure addresses, install routes, enable transit forwarding,
or advertise internet readiness. Other copper ports are outside its scope.

The MP checks the caller's revision and DP boot UUID, requires the packet
transport to be stopped, and drains existing FE100 sessions. CP journals a
temporary ingress redirect before enabling it. A CP systemd timer removes
the owned redirect after 60 seconds even if the MP disappears; failed cleanup
retries every five seconds. Cleanup checks the transaction token and BCM
process/boot identity and cannot clear a newer owner's configuration.

The DP holds the existing exclusive fabric lock and sends at most three DHCP
Discover packets over 12 seconds. Matching checks include physical ingress
source, client MAC, transaction ID, IPv4/UDP validity and DHCP message type.
Only an Offer is accepted; no Request is sent and no lease is acquired. The
MP always requests cleanup, including after an ambiguous SSH result. The CP
records the result only after verified redirect removal. Failures leave
readiness false; saved reports are historical observations, not lease state.

The kernel packet path and 3/4 loop commissioning references are in
[Copper forwarding](../bcm/COPPER-FORWARDING.md). The new diagnostic records
packet counts, source-port counts and EtherTypes, without saving packet
contents. An absent Offer does not establish whether DHCP is unavailable,
an upstream VLAN/MAC requirement exists, or transmitted content was rejected.

## Installation and operation

Install `ffn_wan_forwarding.py` beside CP's existing `ffn_copper_forwarding.py`
and `ffn_faceplate.py` under `/usr/local/sbin`. Install `ffn_wan_probe.py` beside
DP's `ffn_dp_packet_transport.py` and its dependencies under `/usr/local/sbin`.
CP must have systemd-run and the already commissioned queues/header setup.
No switch resource allocation or restart is performed by this feature.

Install `wan_backend.py` in the selected MP management extension alongside
`policy_guard.py`. The updated `install-control-channel.py` registers the
resource. Merge the updated `cli_extension.py` and `static/vif-ui.js` VIF block
into the installed extension, preserving other live changes. Reload the MP
worker after checking its transaction journal. No appliance reboot is needed.

The administrator WebUI exposes the diagnostic within Network > Virtual
Interfaces > WAN1 packet-path test. Read status first; test and recovery use
the same authenticated `/api/system/planes` gateway as the CLI:

```
show platform wan-path
request platform wan-path probe
request platform wan-path recover
```

The protocol resource accepts status, validate and apply. Apply/validate
payloads contain `revision`, `expected_boot_id`, and `operation` (`probe` or
`recover`). Preserve the original UUID when looking up an uncertain request;
do not blindly resubmit a mutation. An unknown outcome can be reconciled
against `config.revision` after inspecting CP state and hardware readback.

## Appliance observation, 2026-09-16

Two bounded tests received no valid DHCP Offer. The second observed 148
frames returned with source BCM28: 138 ARP, nine IPv6, and one IPv4. BCM28's
broadcast transmit counter increased by the three probes. These observations
support WAN ingress/egress activity, but do not qualify DHCP or IP forwarding.
Both tests completed with the redirect disabled, no pending CP transaction,
and FE100 flow admission blocked. The BCM owner was not restarted.

Actual DHCP lease management, a default route, stateful policy/NAT, LAN-to-WAN
tests and restart recovery remain required before internet service is ready.
