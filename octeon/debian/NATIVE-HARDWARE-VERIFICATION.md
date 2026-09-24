# Native Debian hardware recovery and verification

The September 24 recovery restored the PA-5220 CP hardware owners and DP packet
modules after the native Debian transition. **Initialization and the isolated
CPU session-table lifecycle are verified.** Production forwarding and hardware
NAT still require physical packet qualification.

## Fixes

- Reject CP kernels without the Cavium Clause 45 read device-address correction.
  Incorrect reads previously hid valid BCM84848 and gearbox identities.
- Build BDE, BCM, MDIO and DP packet modules against the exact image kernel.
  Build every FE100 adapter with the GNU MIPS64 big-endian userspace compiler.
- Restore DP transport and its NFS root before observing it after a CP restart.
  Recovery still checks the challenged DP boot ID and kernel identity.
- Run the BCM process natively, require persistent owner configuration and the
  reviewed internal SerDes firmware-loading method. Temporary directories are
  rejected. Stage the front-port recipe from the installed platform package.
- Leave all 24 front ports disabled at initialization. Load all four copper PHY
  firmware instances without enabling links or restarting a configured WAN.
- Use persistent native owner-library paths and explicitly include the required
  FE100 adapters and CP hardware control dependencies in new image overlays.
- Provide a selected-platform installer for the MP BCM client modules. Core
  discovers those modules from the installed management extension.
- Reject session operations when FLU, CFP or PRW reports a latched hardware
  error, or an indirect table command has not completed. A ready CPU doorbell
  alone is insufficient, including during warm commissioning.

## Evidence from the current appliance boot

- Native systemd CP and DP; 8 CP cores and 40 DP cores. Both agents report fresh
  acknowledgements. CP restart recovery retained the same running DP boot.
- BCM88375 initialization ready without initialization errors; internal links
  3, 20 and 24 are up (FE100 NIF, FE100 TMI and OCTEON packet trunk).
- Copper PHY addresses 16 through 19 read the expected identity and firmware
  0x1089. All four are administratively disabled; both MDIO write gates closed.
- FE100 DDR training, pattern test and eye test passed after initializing TLU.
  External TCAM configuration/readback passed. FHM, FDT and FCM calibration,
  CFP, FLU and SEM initialization completed without adapter access faults.
- The final isolated session test passed all 12 operations: two initial misses,
  two inserts with exact readback, and both deletes followed by confirmed misses.
  Both hardware flow counters returned to zero, with zero adapter scope faults.
  The result identifies the CP boot and the FE100 reset generation.
- DP packet module initialization completed: PKI, DMA, SSO, PKO and ffnpkt0
  running, with no reported packet-engine or DMA error.
- Running configuration was preserved and configd resumed. Temporary isolated
  port activation was restored to disabled after the test attempt.

## Repeatable read-only verification

On the CP, run:

```sh
env LD_LIBRARY_PATH=/usr/local/lib64:/usr/local/lib64/3p:/usr/local/lib/ffn/owner-deps \
    LD_PRELOAD=/usr/lib/mips64-linux-gnuabi64/libsqlite3.so.0 \
    python3 /usr/local/sbin/ffn_hardware_verify.py
```

Exit zero verifies the stated initialization scope only. The report includes
this boot's identity, live hardware observations and remaining qualification
requirements. Missing copper devices, open MDIO write gates, down internal
links, stale FE100 boot evidence or failed memory prerequisites fail the check.
The command never initializes hardware, sends packets or changes configuration.
`session_table_verified` additionally requires successful lifecycle and cleanup
evidence from the same hardware generation. It is distinct from initialization
and never implies that packets or NAT have passed.

## Session failure and recovery

The first session insert timed out after creating a test entry. The PRW block
had a fatal-error latch and CFP result queues backed up. Clearing that latch
restored the CPU-ready signal, but an indirect fetch still timed out. The test
entry was not claimed as cleaned up at that point.

With the running interface configuration empty and every front port disabled,
the failure journals were preserved in a separate recovery archive. BCM's two
FE100 links were quiesced around the reviewed FE100 soft reset. The reset
cleared the stranded entry; CP, DP and BCM were not restarted. Fresh journals
identify the new hardware generation rather than reusing pre-reset evidence.

The successful rebuild initialized the packet blocks in this order:
TLU, PRW, LAG, QMM, LEF, FWD, DFP, ACL, LIF, PAR, CFP, EGR, IPQ, NIF, TMI.
It then qualified external TCAM/DDR, FHM/FDT and FCM before initializing FLU
and SEM and running the session lifecycle test. This is a commissioning
procedure, not permission to reset or replay initialization on a live firewall.

## Outstanding qualification

The isolated front-port 5/13 test could not proceed: neither member established
carrier while temporarily enabled. The first imported parser had 56 entries and was rejected. The exact 55-entry
reference was then recovered directly from the VM with SHA-256
`6dcbd4fa1e12e5798a55bf22dded9f3fdfc5d0ee90d454d8a4ee88238a1bbddf`.
The initial parser installer refused an unexpected populated baseline. During
the clean rebuild, PAR loaded the owner JSON first; the existing conservative
encoder then installed and verified its expected table without weakening the
baseline guard.
Both test-port SerDes processors and PLLs are healthy but show no receive signal.
A bounded reference transceiver-enable test did not establish carrier; original
expander and port states were restored. Repeat baseline, miss, hit, drop, removal and cleanup checks
for both directions and TCP/UDP address/port translation.

Production session admission remains gated. Do not infer internet-policy
readiness, throughput, physical NAT qualification or full appliance cold-boot
persistence from these results. FE100 memory commissioning remains separate from
the physical-link service; do not replay its destructive stages on a live table.
The native CP still uses appliance-owned ABI libraries and firmware. These are
private deployment inputs, not public redistributable image contents.
