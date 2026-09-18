# Packet queues, DMA submission, and session recovery — 2026-09-14

This change implements bounded software TX queues in the existing OCTEON
backend and durable FE100 session lifecycle recovery. It does **not** commission
PKI/FPA3/SSO/PKO3 or activate FE100 hardware session offload.

## Packet ownership and DMA

`octeon/dpfwd/ffn_dp_io_octeon.c` now keeps a core-local FIFO per egress,
with `DP_BURST` entries per queue. Only WQE ownership metadata is copied;
payloads remain in their original FPA buffers. Queuing transfers ownership out
of the caller's burst so its normal release cannot free a pending packet.

The forwarder calls `tx(NULL, 0)` at the end of each poll. The OCTEON backend
services every egress for at most four passes. A confirmed pre-submission
watermark busy condition permits retry; other rejection results do not.
Remaining software-owned packets are discarded before the next command/policy
processing boundary. Shutdown drops software backlog without issuing new DMA.
The existing AF_PACKET and simulation backends accept the zero-length flush
call without work.

`octeon/dpfwd/ffn_dp_io_octeon3.c` forces real LMTDMA acknowledgements:
SDK 5.1 `cvmx-pko3.h` lines 736–777 otherwise set `rtnlen=0` and manufacture a
PASS result when `CVMX_ENABLE_PARAMETER_CHECKING=0`. The SDK supplies the
publication barrier (`CVMX_SYNCWS`) and completion barrier (`CVMX_SYNCIOBDMA`).
FFN retains the SDK's complete descriptor submission path.

Global packet initialization failure now reaches the multicore barrier before
being propagated, preventing other cores from waiting forever when the init
core returns early. Invalid ingress processing also has a per-poll work limit.

Generic forwarder TX counters count software acceptance. OCTEON `stat_tx`
counts hardware acceptance; neither proves successful transmission on the wire.
Queue counters distinguish accepted, busy, overflow, and expired packets.
These are burst queues, not persistent traffic-shaping queues.

## FE100 session recovery

`fe100/ffn_fe100_journal.py` provides a locked SQLite intent journal with FULL
synchronous commits. `SessionManager(backend, journal)` records bidirectional
intent before the first hardware write, retains ambiguous operations, and
records removal intent before deleting. Restart blocks new installations until
`recover()` removes exact journal-owned entries. Foreign entries are preserved
and recovery remains required. The hardware adapter must also hold the existing
FE100 table lock. Journal-less construction remains available for isolated tests.

No qualified hardware adapter is installed. Session encoding still supports
identity entries only; forwarding/NAT/rewrite actions remain unqualified.

Owner reference findings, from the VM's local sysroot:

- `pan_fe100_flow_entry_t` is a **144-byte C union**; IPv4 member is 120 bytes,
  key at `0x10`, state at `0x20`, flow ID at `0x34`, NAT at `0x40`.
  It must not be passed the existing 64-byte FFN encoded record by casting.
- `pan_fe100_insert_flow_entry` at `0x102da840` calls
  `fe100_send_flow_msg` at `0x102da110` with operation 15. The latter stages a
  command transaction through the `0x48000` block and polls completion.
  It depends on initialized owner runtime state; simply loading that library
  and calling it is not a valid modern adapter.
- Source of evidence: `/mnt/clones/5220-sysroot1-full/opt/dpfs/usr/local/lib64/libpandp_cp.so.1.0`,
  DWARF and disassembly; `usr/share/pdt/fe100.py` diagnostic insert/lookup paths.
  No owner binary is copied into this repository.

## Validation and current hardware limits

- Native VM forwarder, L3/IPv6, ARP, inspection, vsys, and OCTEON backend test
  targets passed. The aggregate `make check` subsequently stopped at its
  big-endian stage because the VM's default cross-compiler was absent.
- Both hardware backends compiled against the local OCTEON SDK 5.1, CN78XX;
  final SDK build log has no warnings.
- The updated OCTEON-III test executable was separately cross-built with the
  Debian chroot's MIPS64 compiler and ran on the actual PA-5220 Debian DP:
  **zero failures**. It uses mock packet hardware, including poisoned freed
  buffers; it does not submit live DMA. New tests cover deferred ownership,
  congestion retry, expiry, full ring/wrap, invalid RX budget, and shutdown.
- **21 Python tests passed on the DP**, including existing link/transport/session
  tests and journal recovery after an actual child-process `os._exit()` during
  the first simulated insert. These do not program FE100 flow entries.
- Live read-only check: BGX2/LMAC0 link and block lock are up, PKND 8, PKI disabled.
  FE100 DRAM and TCAM clock status fields remain zero; `curr_fc_tcam_req` is set.
  No physical forwarding or session offload qualification is claimed.

The next hardware work is Linux-owned FPA3 DMA pool allocation and lifecycle,
PKI style/QPG and SSO receive commissioning, PKO3 descriptor-queue initialization,
and packet tests through the physical trunk. The current kernel's imported
PKO3 sources are not linked because of the old/new WQE API boundary. FE100 then
needs audited FLU/TDI initialization and a bounded command adapter, followed by
bidirectional packet/counter validation before enabling session acceleration.

VM build/evidence directory: `/mnt/clones/ffn-dp-activation-20260914`.
DP tests: `/root/ffn-transport-validation-20260914`.
No production forwarding service was replaced or enabled by this change.
