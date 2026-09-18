# DP packet transport and FE100 session work

## Implemented

`ffn_dp_packet_transport.py` is a direct Linux AF_PACKET transport between
commissioned BCM/FE100 trunk netdevices on the DP and existing front-port TAPs
in `ffn-data`. Packet payloads do not traverse MP or SSH. It supports explicit
selection from all 24 front ports, validates lengths, handles nonblocking
backpressure with counters, and releases descriptors on shutdown. It uses
the same fabric ownership lock as the existing relay and the existing DP
inspection engine before delivering ingress packets to the networking stack.

RX accepts only the previously commissioned FE100 SYSPORT envelope. It rejects
session/control, unknown-flow and exception messages rather than treating them
as Ethernet. TX uses the verified Jericho ITMH/RAW_DSA format. A trunk must be
configured for that format before use. This program does not provision BCM
routes, initialize OCTEON hardware, change NIC features, or automatically assign
ports. It requires an enabled physical Ethernet netdevice with sufficient MTU
and a completed DP Debian/systemd boot. It is not a PKI/SSO/PKO3 DMA driver.

`fe100/ffn_fe100_sessions.py` implements IPv4 TCP/UDP flow key and identity-entry
wire encoding from the VM owner's `pcs/packets/fe100.py`, and a two-direction
session lifecycle with preflight ownership checks, readback, partial-install
rollback, policy-revision invalidation and recovery-required state for ambiguous
cleanup. Unsupported rewrite/action bits are rejected. It does not treat wire
bytes as the C library's `pan_fe100_flow_entry_t` ABI.

**The session manager currently has no qualified hardware backend and is not
integrated into live policy processing.** Its tests use an in-memory adapter.
It does not yet program FE100 sessions, bypass software inspection, or provide
production crash recovery. A persistent ownership journal, actual hardware
adapter, aging/counters and verified forwarding actions remain required.

## Validation

- Ten unit tests passed on the VM and on the PA-5220's big-endian MIPS64 CP.
- `test-dp-transport-kernel.py` passed in an isolated network namespace on MP:
  real AF_PACKET receive/transmit with exact bytes, inspection rejection and
  control-message rejection. Datagram socket pairs stand in for TAP handles
  in this test; it is not a physical DP/FE100 test.
- Live read-only FE100 samples still show all four external lookup clocks at
  zero and current TCAM request flow control. Table 4 (FWD) selects external
  memory. No session table writes or reset were performed.
- DP console reports PID 1 `init`, Debian files in PID 1's root, and only
  `lo`, `sit0`, `ffndp0` netdevices. DP SSH refuses connection. `ffndp0` is
  the management transport, not an eligible physical packet trunk.

## Activation work still required

1. Complete DP boot handoff and restore its management endpoint.
2. Bring up the DP's physical OCTEON packet interfaces, with verified queue,
   buffer and DMA ownership. The existing CVMX backend is a separate path from
   this Linux netdevice transport; neither may take ownership from the other.
3. Commission BCM DP trunk ingress/egress formatting and port routing. Verify
   actual front-port traffic, drops, MTU, admin/link transitions and recovery.
4. Audit and initialize FE100 external lookup memory/clocks, then resolve the
   previously observed parser/ACL lookup stall in a recoverable test sequence.
5. Implement the hardware session adapter against verified C ABI or hardware
   message semantics. Prove bidirectional hits, deletion, aging, policy changes
   and software fallback with real packets before asserting offload readiness.

New modules are staged for validation only. No transport/offload startup service
was enabled and no forwarding configuration was changed by this work.

## Follow-up: DP Debian activation

The DP now runs `6.18.49-ffn-debian-dp-20260914+` with systemd as PID 1,
the same Debian root as its agent, and all 40 CPUs detected. SSH, network and
OCTEON crypto services are active; `systemctl --failed` reports zero units.
The saved network revision 60 was retained. The AES driver reports
`selftest: passed`. All ten transport/session unit tests passed on the DP.

The first handoff candidate exposed a missing bridge feature; the final
candidate uses the saved router configuration and matching kernel modules.
It is selected in the CP's `ffn-dp-boot.service.d/zz-router.conf`; the previous
selection is backed up beside it with suffix `.before-20260914`.

Final image in the CP compatibility root:
`/opt/ffn/ffn-vmlinux-systemd-dp-router-20260914`.
SHA-256: `5dc48272447462b88539aa47b8d1aa703ed81b5a56344508fd288011d9efafd9`.
The VM's isolated sources, build tree, modules and logs remain under
`/mnt/clones/ffn-dp-activation-20260914`.

Build/boot corrections:

- `build-systemd-boot.sh` accepts DP-only builds, an explicit saved `DP_CONFIG`,
  and `DP_EXPORT` (default `/opt/dproot`). It uses repository initramfs sources,
  checks the handoff helper/export, and builds matching modules.
- `ffn-dp-boot.sh` verifies the image before stopping the management transport
  and attempts to restore that transport on exit. A missing-image test left
  the live transport active.
- `dpboot8.sh` supports skipping the legacy mailbox-agent wait when the Debian
  supervisor owns readiness. This lets CP start the link before DP needs it
  to mount its NFS root. This ordering patch was installed after the candidate
  boot tests; a subsequent full boot through this patched path remains to be
  verified. The candidate handoff itself was observed on the appliance.
- The crypto startup script prefers a module installed under the running
  kernel's version, preserving the older fallback for older installations.

The boot/SSH blocker listed above is resolved. Physical DP packet interfaces
are still absent: the new `p1`, `p3`, `p5`, `p13` interfaces are TAPs in the
network namespace, not verified OCTEON packet ports. FE100 external lookup
initialization and a qualified session hardware adapter remain unfinished.
