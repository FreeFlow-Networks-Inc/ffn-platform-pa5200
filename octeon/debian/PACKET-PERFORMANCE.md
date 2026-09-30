# OCTEON packet processing performance

The direct BCM/OCTEON attachment currently sends transit packets through Linux
TAP interfaces and the stateful policy/NAT engine. BCM aggregate member selection
does not establish hardware NAT or hardware security enforcement.

The first optimization removes work before the existing policy path:

* AF_PACKET receive sockets admit only their commissioned OTMH source ports.
  Aggregate filters include the fixed logical-system-port aliases. Full envelope
  decoding, LACP collection/distribution gates, inspection and kernel policy
  remain in place.
* Transmit-only sockets use protocol zero and receive sockets exclude outgoing
  copies. They no longer queue copies for unrelated interface owners.
* Aggregate VLAN/address lookups and physical source maps are compiled when the
  owner accepts configuration, instead of reparsing addresses for every packet.
* Physical owners drain bounded 64-packet bursts in each ready direction.
  Inspection runs for every received transit packet. Empty nonblocking queues
  do not increment packet-drop counters; ambiguous writes are not retried.

## Verification

On 2026-09-29, 23 packet/aggregate tests passed on the live MIPS64 Debian DP and
the x86-64 Linux build host. The isolated network-namespace test also exercised
the real kernel socket ABI, physical and LAG source admission, byte-exact
transmission, outgoing exclusion and transmit-only receive exclusion. It does
not change production interfaces or routing.

An OCTEON CPU-time microbenchmark compared the previous revision with cached
classification, using 50,000 tagged packets and IPv4/IPv6 interface addresses:

| Operation | Previous packets/CPU second | Cached packets/CPU second |
| --- | ---: | ---: |
| VLAN and local-address classification | 3,204 | 69,392 |
| OTMH source decoding | 60,401 | 122,801 |

These measure individual functions, **not end-to-end forwarding throughput**.
They do not establish 1G, 10G or 100G forwarding. Hardware NAT remains unqualified.

The native trunk driver still has one receive worker, bounded TX slots and a
fixed receive polling sleep. High-rate validation must include this driver,
policy/NAT, bidirectional loss, latency, packet sizes, interface errors and
restart/recovery behavior. Installed optic speed is not proof of board capability.

To run the kernel socket verification on either architecture:

```sh
sudo python3 octeon/debian/test-packet-socket-linux.py
```

Use the same directory containing `ffn_packet_socket.py`. The test creates a
private network namespace; its veth interfaces disappear when the test exits.
