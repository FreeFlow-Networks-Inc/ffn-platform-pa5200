# Physical forwarding and session-offload validation

**Result: blocked by incomplete implementation. No physical forwarding or
hardware-session test passed.** This is separate from the successful TCAM
configuration/recovery verification.

`validate_packet_hardware.py` ran on the live PA-5220 DP. It exited2 to indicate
blocked prerequisites. Exact live results are in
`PHYSICAL-VALIDATION-20260914.json`.

| Component | Live observation | Functional validation |
|---|---|---|
| Debian DP and internal BGX link | Ready; link up | Prerequisite present |
| DMA pools | Prepared, no recorded error; capacities512/570/1024 | No new RX/TX DMA exercise |
| PKI |1001 microcode words prepared; PKI enable0, active0 | RX DMA test not run |
| SSO | XAQ memory prepared; AW_CFG0xe, RWEN0 | External queue operations disabled |
| PKO3 | Queue topology and memory prepared; PKO enable0 | TX/completion test not run |
| Physical Linux packet interface | None; only lo, sit0 and management TAP ffndp0 | AF_PACKET forwarding test blocked |
| Native FE100 session adapter | Not connected to FFN session manager | Insert/hit/miss/delete test not run |

The real transport trunk validator rejected `ffndp0`, preventing a management
TAP test from being reported as physical packet validation. Absence of a Linux
netdevice blocks the AF_PACKET transport; a properly commissioned raw CVMX
path would not itself require a Linux netdevice. That separate SDK path is
not installed/running on the inspected Debian DP either.

## Implementation findings

- `octeon/kctl/ffn_dp_packet_init.c` exposes preparation operations only. It
  does not activate packet ingress/egress or install a packet consumer.
- `octeon/kctl/ffn_dp_dma.h` deliberately leaves SSO RWEN clear and PKO DQ
  closed. SSO RWEN specifically enables external XAQ operations, as documented
  in the VM SDK `executive/cvmx-sso-defs.h`; it is not a blanket SSO-enable bit.
- `octeon/dpfwd/ffn_dp_io_octeon.c`, `oct_io_to_offload`, increments
  `stat_offload` and frees the WQE/buffer. It does not deliver packets to FE100.
  That counter is not evidence of hardware offload.
- `fe100/ffn_fe100_sessions.py` implements a flow wire codec and session
  lifecycle. Its64-byte record is explicitly not the native FE100 C entry ABI.
  The inspected production tree has no adapter wiring; `SessionManager`
  instantiations found by the audit are in tests only.
- The SDK backend source contains raw CVMX initialization and packet paths,
  but running global SDK initialization blindly would conflict with the Linux
  driver's existing DMA ownership. Validation did not do this.

## Work required before the requested tests can execute

1. Finish one coherent DP packet backend with PKI style/QPG/aura mapping,
   SSO consumers, PKO MAC credits, queue-open and completion handling, sharing
   the existing Linux DMA ownership correctly.
2. Replace the FE100 punt placeholder and connect the native session adapter
   with verified entry encoding, acknowledgement and readback.
3. Exercise real front-port packets in both directions, verify transmitted
   bytes and RX/TX completion/counters, then verify session hit/miss,
   deletion, policy invalidation and fallback behavior.

This validation did not activate forwarding, install sessions, reset hardware,
reallocate DMA pools, assign ports or push code. No mock tests are counted as
physical forwarding or hardware-session validation.
