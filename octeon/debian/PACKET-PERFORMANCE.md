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

## Receive steering on the TAP devices (2026-10-09)

Measured on the PA-5220 with the LAN on the ae1 aggregate and the WAN on
p1 (Cox), one 100 MB HTTP download from a LAN host through the box, while
sampling the dataplane CPUs once a second:

| Configuration | One flow | Four flows | Busiest core |
| --- | ---: | ---: | --- |
| no steering | 42.6 MB/s (341 Mbit/s) | not run | cpu1 (WAN receive thread) 88-97% |
| RPS on p1, p5, p2, ae1 | 53-55 MB/s (440 Mbit/s) | 63.3 MB/s (506 Mbit/s) | cpu6 (ae1 transmit thread) 62-64% |

Without steering the owner's receive thread forwards every frame inside
its TAP `write()`: VLAN demux, conntrack, the policy and NAT tables,
routing and the egress TAP queue all ran on cpu1, and that one in-order
OCTEON core was the ceiling for the download direction. With receive
packet steering the kernel hands each frame's forwarding to another CPU by
flow hash (flows stay ordered), the writer keeps only the copy, and the
WAN link became the limit: four flows did not raise the busiest core above
65%.

`ffn_tap_steering.py` computes and applies the mask; a native owner applies
it to its TAP when it starts its workers (`PacketOwner.steer`) and never
fails an attachment over it. The steering CPUs are the upper half of the
CPUs the owner may run on, less CPU 0 and every reserved worker CPU, so on
the 40-core dataplane with four owners they are CPUs 24-39. Machines with
fewer than eight eligible CPUs are not steered.

The next limit is the egress side: the aggregate's transmit thread reads
the ae1 TAP one frame per `read()` and reached 64% at 62k packets per
second, and the TAP's 1000-frame queue dropped 479 frames during the
four-flow run. Beyond roughly a gigabit per direction the TAPs need
multiple queues with one reader per queue (`IFF_MULTI_QUEUE` and
`PACKET_FANOUT` on the trunk socket), which is a change to the native
driver, not to steering.
