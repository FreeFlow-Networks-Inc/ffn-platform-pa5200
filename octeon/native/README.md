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
* `ffn_native_aggregate.py` uses the same workers for aggregate data frames.
  Native C owns member/SPA admission, VLAN classification, local-address
  inspection exceptions, per-unit counters and the existing CRC32 rendezvous
  member selection. A separate native socket filter delivers only LACP frames
  to the Python control loop. Python retains negotiation and configuration.
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
IDs, selected CPUs and faults. These are parallel directions, not multiple RX
flow queues; no multi-flow scaling or hardware acceleration is implied. Ownership locks,
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

## Validation

```sh
make all test
sudo python3 test_kernel_packet.py
```

Thirteen physical packet tests, ten aggregate tests, six CPU allocation tests, a C hardware-transaction
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

## Migration still required

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
