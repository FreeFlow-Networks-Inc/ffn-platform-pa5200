# Hardware aggregate control development

Aggregate Ethernet status now belongs to the selected platform and MP control
daemon. The core endpoint `/api/interfaces/aggregate-status` invokes the platform
hook declared by `aggregate_status_version: 1`. A selected provider failure
returns 503 instead of searching for a management-CPU bond. Generic installations
retain the Linux bond status implementation.

`show platform aggregates` uses that same authenticated endpoint. The Network >
Interfaces > Aggregate Ethernet table displays mapped BCM member IDs, observed
link speeds, candidate versus committed intent, and explicit activation blockers.
It does not tell an operator to create a Linux bond on the MP for BCM ports.

## MP configuration contract

`aggregate_config.py` parses the canonical candidate/running XML used by the WebUI
and CLI. It resolves `aggregate-group` references, maps physical members, validates
member counts, addresses, DHCP/static conflicts and protocol options, and retains
DHCP default-route intent. `configd_applier.py` uses the same compiler to explain
why a group/member cannot apply. Existing running intent is not silently rewritten.

`aggregate_backend.py status` reads both configuration revisions and obtains
faceplate, network attachment and passive LACP observations through fixed helper
commands. Configuration changes during sampling cause a retryable failure. Reads
may run concurrently; there are no aggregate mutation commands in this version.

The MP worker configuration selects resource `aggregates`, action `status`, with
argv `["/opt/ffn-ngfw-v2/venv/bin/python",
"/opt/ffn-platforms/pa5200-management/aggregate_backend.py", "status"]`.
Keep the interpreter and platform directory appropriate to the installation.
The control request follows `ffn-controld -> MP worker -> fixed platform helpers`.

## Passive DP protocol observation

Install `ffn_lacp_packets.py` and `ffn_lacp_observer.py` beside the existing
`ffn_dp_packet_transport.py` on the DP. Install `octeon/debian/ffn-lacp-observe-mp`
as executable `/usr/local/sbin/ffn-lacp-observe` on the MP. The helper uses the
existing pinned plane connection and accepts no remote command from clients.

The DP observer opens a separate AF_PACKET socket on the existing `ffnpkt0`
trunk, with a one-second/4096-frame bound. It ignores locally transmitted frames,
requires the commissioned OTMH source-port envelope, and validates LACP version,
TLV boundaries and actor identity. It decodes actor/partner system, key, port and
state flags. The wire layout follows the upstream
[Linux LACP definitions](https://github.com/torvalds/linux/blob/master/include/net/bond_3ad.h).
No SDK code or binary data is copied into this implementation.

Observations distinguish receive counts, invalid PDUs, timeout/expiry and partner
mismatch. MP processing delay is added using monotonic time; CP/DP wall clocks
are not assumed synchronized. A received partner advertisement never asserts
that the local system negotiated or programmed forwarding. Empty samples mean
no LACP frame reached the observer during that window, not that no peer exists.

The observer sends no packets, provisions no redirects, and changes no interfaces,
registers, packet-owner state or configuration. The existing WAN owner stays up.

## Work still required for activation

This increment provides configuration validation, accurate status and passive
protocol decoding. It does **not** make an aggregate operational. Activation
continues to fail explicitly until all of these have a verified implementation:

1. BCM aggregate ownership, hashing and selected-member readback/rollback.
2. LACP packet delivery/transmission, receive/periodic/selection/mux state
   machines, partner agreement, timeout withdrawal and link-event handling.
3. Aggregate ingress/egress attachment to the OCTEON dataplane, preserving
   inspection, routing and local management profiles.
4. DHCP lease lifecycle and LLDP when requested by the configuration.
5. Physical peer qualification, traffic tests and single-member failure tests.

Do not set a qualification flag to bypass these requirements. Hardware tests
must preserve the existing WAN path and verify both member-specific control
traffic and aggregate forwarding before reporting applied state.
