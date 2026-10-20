# Native runtime boundary

Production packet bytes and hardware transactions belong in C/C++. Python owns
configuration validation, lifecycle, control protocols and telemetry publication.
Do not add Python packet-forwarding loops, direct MMIO or device ioctls.

Implemented here:

* `libffn-packet.so` owns physical-port RX/TX, kernel socket filtering, namespace
  and TAP setup, bounded polling, OTMH/ITMH framing, padding, local-address
  classification, native inspection calls and counters. It does not bypass
  the existing kernel Security/NAT enforcement. It is CPU forwarding, not
  hardware offload.
* `ffn_native_packet.py` is a control ABI. Two pinned native workers process
  receive and transmit independently. Python sees settings and counters only,
  never forwarded packet buffers. CPU selection uses the process affinity mask.
* `ffn-portd` (`ffn_portd.c`) is the dataplane port owner, one daemon with two
  owner kinds. `physical` owns one faceplate port's packet path, WAN port 1
  and ports 2..24, copper and optical alike (`ffn_wan_runtime.py` and
  `ffn_physical_runtime.py` exec it under the units): the TAP's native
  workers, the local addresses from the network owner's journal, the FE100
  return bindings and, for a port whose carrier policy is `observed` (copper
  2..4 by default, `/etc/ffn/port-carrier.json` overrides), TAP carrier and
  delivery that follow the CP's faceplate observation under a lease. The MP's
  `ffn-port-links.timer` delivers that observation through `ffn-portd links`;
  what the copper VIF driver did for those ports is this lease, and the VIF
  owner is gone. The same delivery is written a second time as the route
  tracker's link observation, `/run/ffn-route-links.json` (`{boot_id,
  revision, links: {pN: bool}, observed}`, a port up when its row says
  available, enabled and link), so the tracked default routes no longer
  depend on the MP's separate `ffn-route-link-feed` (2026-10-20: that feed
  stayed stopped after a patch install and the routes were withdrawn for
  fifteen minutes); `test_portd_links.py` proves the record without hardware
  and `test-portd-linux.py` against real owners. `aggregate` owns one aggregate: the
  LACP engine, the member gates, the trunk control socket and the aggregate
  TAP's native workers in one process, under the MP supervisor's JSON-line
  contract (`ffn_aggregate_runtime.py serve` execs it). Its life is bound to
  that pipe: EOF, a controller silent for eight seconds, SIGTERM or the death
  of its parent all withdraw the gates, release every lock and remove the TAP
  within seconds, and a cleanup that hangs is cut off after a minute for the
  watchdog to finish. The kernel interface transactions (TAP, nftables guard,
  addresses, VLAN units, DHCP hooks) stay with the Python helpers in
  `ffn_aggregate_runtime.py`, run as bounded one-shot processes (`validate`,
  `network-setup`, `network-update`, `network-teardown`); so does the shared
  worker-CPU allocator (`ffn_packet_cpus.py reserve --pid`), which records the
  daemon as the owner. `ffn_native_aggregate.py` remains the control ABI the
  tests exercise; no production owner imports it.
* `ffn_lacp.c` is the LACP engine and LACPDU codec, transport independent and
  state for state the C form of `ffn_lacp_engine.py` / `ffn_lacp_packets.py`.
  The Python suites are its conformance tests: `make test` runs
  `test_lacp_engine.py` against it through `ffn_lacp_native.py`
  (`FFN_LACP_ENGINE=native`), and `test-lacp-engine-linux.py` negotiates it
  against the Linux bonding driver the same way. The native data path owns
  member/SPA admission, VLAN classification, local-address inspection
  exceptions, per-unit counters and the existing CRC32 rendezvous member
  selection; a separate native socket filter delivers only LACP frames to the
  owner's control loop.
* Aggregate gates expire in C at the earliest active member's carrier or LACP
  deadline, even while Python is stalled. The conservative group withdrawal
  also preserves minimum-link requirements. A leased BCM egress acknowledgement
  may select the hardware trunk; expiry falls back to the eligible software
  member set. This capability is not production L3/NAT offload qualification.
* `libffn-hwio.so` owns bounded, atomic Linux I2C transactions and checks complete
  transfer results. Optics and fan-control Python modules invoke that ABI.
* The image builder compiles these libraries and the existing native inline
  engine with the Debian MIPS64 big-endian userspace toolchain. Missing native
  libraries fail startup; there is no Python packet fallback.

Packet ABI 2 has one control owner and ordered RX/TX workers. Configuration and
engine-handle replacement require both workers to acknowledge a pause barrier.
Close joins the workers before releasing descriptors or the inspection engine.
A three-second monotonic control lease stops both workers if control disappears;
fatal worker errors also stop the pair. Telemetry exposes actual worker thread
IDs, selected CPUs and faults. Ownership locks,
boot identities, stateful policies and interface management permissions remain
mandatory. Unsupported/malformed inspection verdicts retain the existing engine
semantics and separate counters; this is not a claim of full IPS coverage.
Native event descriptors wake paused workers immediately instead of waiting
for the packet poll timeout. Aggregate configuration transactions withdraw data
delivery while the network worker applies changes; LACP control continues.

RX drains up to 64 frames per nonblocking `recvmmsg` call into reusable native
buffers. The bounded batch preserves ordering, per-frame admission, truncation
checks and the control lease. Worker telemetry includes receive syscall count,
frames returned and largest batch; batching does not imply RSS or multiple RX
queues. `ffn_packet_cpus.py` allocates RX/TX cores under a shared reservation lock,
within the process affinity mask. It reserves the first allowed core for control
when at least three cores are available, balances oversubscription, and respects
live legacy workers during rolling upgrades. Reservations expire on process
death, PID reuse or reboot and are released only after native workers join.

### Lanes

An owner may run lanes. Each lane is one receive socket in the trunk's fanout
group, one queue of the TAP and one transmit socket, with a receive worker and a
transmit worker of its own; lane 0 is the owner as described above, and the owner
still pauses, resumes, configures and closes as one unit. The kernel's fanout is
by receive queue (`PACKET_FANOUT_QM`): the trunk driver records the SSO group a
flow hashed to as the frame's receive queue, so a flow stays on one lane, in
order, while flows spread over lanes. Each lane writes its own TAP queue, so no
lock serializes the lanes in the kernel either. `ffn_packet_lanes` grows an opened
owner before its workers start and answers with the count reached: one when the
TAP was not made with `multi_queue`, or when the owner was adopted from caller
descriptors. `ffn_packet_start_lanes` pins lane i to `rx_cpus[i]` and
`tx_cpus[i]`; `ffn_packet_lane_workers` reports a lane's workers. The FE100
return filter is applied to every lane's socket. With a trunk driver that records
no queue, every frame reaches lane 0 alone, which is merely the old behaviour.

The Python owner takes its lane count from `/etc/ffn/packet-lanes.json`, for
example `{"default": 2, "p5": 4, "ae1": 4}`; no file means one lane. Two CPUs
are reserved per lane, never the trunk driver's receive cores (its status names
them) while enough others remain, and the receive steering of the forwarding
path keeps off those cores too. The runtimes make their TAPs with `multi_queue`;
the VIF runtime makes a device again when it finds one made without it.

## Validation

```sh
make all test
sudo python3 test_kernel_packet.py
sudo python3 test-portd-linux.py
```

`test-portd-linux.py` runs `ffn-portd` under the MP contracts in two
disposable namespaces: a veth as the trunk, the real native library on a real
multi-queue TAP, the Python reference engine as the LACP peer, and the
kernel-interface helpers faked. It proves the first status row, LACPDUs in the
trunk envelopes both ways, both members distributing against the reference
engine, and that a closed control pipe, a silent controller and SIGTERM each
end the owner within seconds with its TAP and run files gone.

Fourteen physical packet tests, ten aggregate tests, six CPU allocation tests, a C hardware-transaction
test, and private network/mount
namespace integration cover native framing, source admission, bounded lengths,
inspection blocking, configuration replacement, IPv4/IPv6 local services,
padding, worker order, configuration barriers, lease expiry, actual CPU affinity
and TAP ioctls. Packet tests and kernel integration passed on
x86-64 and the MIPS64 Debian DP. The test scanner is never installed in images.
The aggregate tests cover VLAN-only parents, stable member selection against the
previous implementation, acknowledgement expiry, network transitions and
control-wrapper updates without replacing the TAP or worker threads. Real
AF_PACKET integration verifies separation of data and LACP receive queues.

Physical-port transmit uses preallocated `sendmmsg` batches of up to 64 packets.
The worker immediately flushes the packets already available; it never waits to
fill a batch. Partial sends advance past the accepted prefix, and a full socket
queue counts every dropped suffix packet without replay or unbounded retries.
The additive `ffn_packet_transmit_stats` API reports socket calls, accepted frames
and largest accepted batch. The 80-frame fixture verifies two calls, byte-exact
framing and padding; a constrained real datagram queue verifies partial sends,
order and exact drop accounting. Aggregate transmit retains its existing path.

## Migration still required

### FE100 original-packet return

The additive `ffn_packet_fe100` API attaches up to eight explicit return bindings
to a paused physical or aggregate owner. Each binding includes the BCM return
port, front port, ingress LIF, zone and existing software source mapping. Socket
filters admit that return only after configuration. A maximum three-second
monotonic lease must be renewed by the trusted attachment controller. Expiry
drops returned envelopes; it does not authorize or withdraw hardware flows.

The C decoder accepts physically verified IPv4 TCP/UDP FLOWUNKNOWN, TTL-one,
MTU and first-fragment exceptions, and the type-2 session punt: the TCP control
segments (FIN, RST) of a flow the chip carries, handed up as the original
packet with the flow ID and the ingress zone (PA-5220, 2026-10-19). Those must
reach the kernel path so conntrack leaves ESTABLISHED and the session lease is
withdrawn; they are never rewritten forwarding messages. It validates envelope lengths, message type,
ingress identity and available tuple metadata. Unsupported formats are rejected.
The original Ethernet packet goes through the existing member collection,
VLAN/network, inspection and kernel policy path. It is never delivered through
Python. Separate counters report decoded, rejected and expired returns. The owner's kernel filter hands it only the returns whose packet info names one of its own
front ports (after the IPv4 or IPv6 key of a FLOWUNKNOWN message, or directly for exceptions and control), so a
shared return port costs one copy per message and a rejected return is a malformed message for a front the owner
holds, never another owner's traffic. `ffn_packet_filter_attach` installs that filter on any datagram socket.

`ffn-fe100-punt-probe` is a bounded, receive-only commissioning consumer using
the same native owner and real AF_PACKET filter. Its TAP/TX endpoints are local
socket pairs, so it cannot transmit or forward captured traffic. It requires a
per-run nonce and explicit binding. `test_fe100_punt.c` retains real isolated
wire fixtures; `test_fe100_packet.py` exercises delivery, inspection, aggregate
member withdrawal, malformed envelopes, lease expiry and control validation.

Production packet owners do not automatically enable these bindings. Their
attachment controller must commission the BCM/FE100 path, verify the live DP
acknowledgement and withdraw ingress before a failed/expired receiver can
blackhole packets. See `fe100/PHYSICAL-RETURN.md` for measured scope and remaining
activation requirements.

* Legacy commissioning forwarding paths in `ffn_fabric.py`,
  `ffn_dp_packet_transport.py` and other lab/relay tools. The physical production
  owner no longer calls their packet loops.
* Direct MMIO utilities: FE100 diagnostic BAR access, chassis/fan override and
  port LED helpers. Existing native FE100 and BCM drivers/adapters are reused;
  Python control clients must not grow new register-access implementations.
* Remaining direct ioctl users, including discovery/diagnostic tools. Convert
  behind bounded native operations with the current ownership and readback checks.

Physical and aggregate production owners now use native forwarding. The
repository-wide conversion and parallel receive-queue work remain incomplete.

## Throughput target and qualification

The target is 100–200 Gbit/s forwarding before optional inspection. Report each
direction separately and distinguish their sum from one-direction throughput.
Measure offered and received wire rate, loss, latency, frame sizes and flow
count; packet injection counts or link speed alone are not throughput evidence.
The deployed CPU path currently uses one SSO receive group and a configured
40G BCM-to-DP trunk. That path cannot establish 100G one-direction forwarding.
The driver now skips its fixed idle sleep when a complete 64-packet RX budget
was consumed, while retaining scheduler yields, TX reaping and fault checks.
This does not add receive queues or widen the hardware transport.

DP kernels must enable `CONFIG_HIGH_RES_TIMERS`. On the measured low-resolution
HZ=100 kernel, a requested 0.5ms sleep took approximately 10ms. Sleeping after
every 64-frame receive batch can limit service to about 6,400 frames/s regardless
of spare CPU capacity. The image builder rejects DP configurations without
high-resolution timer support; the isolated kernel builder enables it. A booted
image still needs timer-resolution and wire-throughput verification. Kernel or
pinned DMA-driver replacement requires a DP restart.

BCM/FE100 admission still requires verified bidirectional forwarding, exception
handling and ordered policy/route/neighbor withdrawal. A forwarding-only lab
benchmark must be explicitly isolated; disabling inspection never disables
configured security or NAT. No 100–200G result has been qualified yet.

FE100 compact FLOWSTATS messages now have a native C decoder and bounded
`ffn-fe100-stats-probe` receiver. Isolated NAT tests matched every packet/byte
delta, including paired TCP records. The packet owner consumes valid statistics
without delivering them to a data TAP or counting them as malformed original
packets; a single separate accounting owner must consume each delta once.
See `fe100/COUNTERS.md` for the format and production lifecycle gaps.
