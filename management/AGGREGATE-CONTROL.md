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

## Negotiation engine and OCTEON transport

`octeon/debian/ffn_lacp_engine.py` implements original, transport-independent
LACP receive, periodic transmit, selection and collection/distribution logic.
It supports active/passive operation, fast/slow receive timers, the peer's
requested transmit period, a two-second aggregation wait, minimum links and
member withdrawal on carrier loss, speed change, lease expiry or peer timeout.
Selection keeps a consistent partner system/key and speed; duplicate partner
port identities are excluded. A peer must echo the local identity and advertise
synchronization before collection; distribution additionally requires the peer
to advertise collection. Transmissions are limited to three attempts per second.

The engine does not program hardware. Its owner must supply:

- One stable unicast system MAC, a unique aggregate key and physical member IDs.
- Monotonic timestamps and fresh, verified full-duplex carrier/speed observations.
  `link(port, up, speed_mbps, now)` grants a three-second observation lease.
- A synchronous gate driver whose `apply()` programs **and reads back** each
  member's collection/distribution state. Returning the requested mapping without
  enforcing the data gates is not a valid hardware driver.
- A regularly serviced event loop. `transmissions(now)` advances timers and
  yields Ethernet LACPDUs; `receive(port, frame, now)` advances the receive state.

Initialization first closes all gates. Driver failure/readback mismatch latches
a fault and attempts withdrawal, stops advertisements and never automatically
re-enables members. `stop()` retries withdrawal even after a fault. A stopped
owner cannot enforce future timeouts: hardware activation also needs a watchdog
or equivalent lease-enforced data gate that closes if the owner stops running.

`ffn_lacp_trunk.py` adapts this engine to the existing packet owner's commissioned
OTMH_SSP ingress and ITMH/RAW_DSA egress. It accepts the optical member map only,
preserves physical ingress identity, ignores outgoing observations, consumes
invalid slow-protocol frames and directs every LACPDU to its physical member
instead of hashing it through an aggregate. Short/failed sends fault the engine
and attempt gate withdrawal. It opens no second packet socket and installs no
BCM redirect. The packet owner must call it before data inspection/delivery;
the adapter does not itself supply a hardware gate driver or packet-owner loop.

Install `ffn_lacp_engine.py` and `ffn_lacp_trunk.py` beside the packet codec and
existing DP packet transport. Installing these modules starts no service and
does not change the passive observer, configuration, BCM programming or packet
ownership. They are building blocks for the commissioned aggregate owner.

## Repeatable negotiation tests

Run the unprivileged suites from `octeon/debian`:

```sh
python3 -m unittest test_lacp_packets test_lacp_engine test_lacp_trunk
```

The same suites run on x86 Linux and native MIPS64 big-endian Python. CI runs
these explicitly, including port-envelope byte order and failed-send withdrawal.
The trunk suite requires Linux; it uses fake sockets and no physical devices.

On a Linux test host with root, `ip`, `ping`, bonding, veth and TUN/TAP available:

```sh
sudo python3 test-lacp-engine-linux.py --activity active
sudo python3 test-lacp-engine-linux.py --activity passive
```

This creates two temporary network namespaces. The FFN engine gates a software
TAP adapter on one side; the other side uses the real Linux bonding driver.
The test verifies two-member agreement on both ends, bidirectional IPv4 traffic,
withdrawal after missing LACPDUs without a carrier change, renegotiation, and IP
forwarding with each member as the only surviving link. Its TAP ioctl uses the
native MIPS ABI where required. Cleanup deletes only namespaces created by that
invocation. No physical port, production route or BCM setting is changed.
These tests prove protocol/software interoperability, not hardware commissioning
or aggregate throughput.

## Work still required for activation

The tested engine and packet-envelope adapter do **not** make a physical
aggregate operational. Activation continues to fail explicitly until all of
these have a verified implementation:

1. BCM aggregate ownership, hashing and selected-member readback/rollback.
2. Attach the tested LACP engine to the real packet owner, per-member control
   packet traps, leased hardware link observations and acknowledged hardware
   data gates, including withdrawal on owner failure.
3. Aggregate ingress/egress attachment to the OCTEON dataplane, preserving
   inspection, routing and local management profiles.
4. DHCP lease lifecycle and LLDP when requested by the configuration.
5. Physical peer qualification, traffic tests and single-member failure tests.

Do not set a qualification flag to bypass these requirements. Hardware tests
must preserve the existing WAN path and verify both member-specific control
traffic and aggregate forwarding before reporting applied state.
