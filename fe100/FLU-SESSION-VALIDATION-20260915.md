# FLU initialized; CPU session-table lifecycle verified

Follow-up: [physical IPv4 UDP session-action validation](PHYSICAL-SESSION-OFFLOAD-20260915.md)
now passes for front-port ingress with MP capture egress. The original CPU-only
results below are retained as the earlier milestone; production offload remains gated.

The PA-5220's native FLU initialization completed for cfg4, usecase1 and
detected FDT capacity4. Both directions of an isolated UDP session were
inserted, fetched with matching keys/state/flow IDs, deleted, and confirmed
absent. Zero register-scope faults occurred. Hardware session forwarding
through physical ports is still unverified.

## Reference and implementation

All initialization decisions reference VM
`/mnt/clones/5220-sysroot1-full/opt/dpfs/usr/local/lib64/libpandp_cp.so.1.0`,
SHA256 `b57227a460144c8c2545fc2e268b31f475ef72ac2a6f1d457387ab46d842c3e9`.

`ffn_fe100_flu.py` restores the four248-byte DDR configuration slices from
successful, current-boot training journals. It also restores the capacity at
full configuration offset48 from FDT1's detected configuration. Reusing the
template's capacity3 would select the wrong table sizes. The initialized
configuration has usecase1, cfg_mode4 and v4_v6_choice2. Each DDR configuration
has the native optional Vref sweep disabled; the wrapper rejects a profile
that would require additional DDR writes.

The wrapper calls native `pan_fe100_set_flu_config` at0x102b5ed0, including
`pan_fe100_flu_cfg_usecase` and `pan_fe100_flu_init_mems`. A120-second native
watchdog bounds execution. Writes are limited to known FLU registers;
TDI, DDR and packet blocks remain outside the write mapping. The initializer
requires empty tables and an unused per-boot journal; it never repeats an
interrupted initialization automatically.

Cfg4 initializes11 targets, masks1 through0x400, producing init_status0x7ff.
FFN's earlier check incorrectly required all20 possible target bits. Native
`fe100_ia_init` uses access type4 and block8; verification checks that every
command receives IA completion code1, rather than counting submissions.

Native `pan_fe100_flu_init_mems` clears CPU-access mode at0x102b2fc8 before
normal operation. The owner does not program the legacy reset fields at
0x40404. Those fields and CPU-access mode were therefore removed as session
readiness requirements. Actual FHM/FDT clocks, successful calibration
journals and live controller normal-mode bits remain required. A successful
current-boot FLU initialization/verification journal is also required.

## Initial wrapper check and read-only recovery

The native initializer returned success with zero scope faults, but the first
wrapper marked its journal failed because it compared whole DDR status words
while FLU was exercising memory. Subsequent reads show that these status
words can vary while their required ready/normal-mode bits remain set.
The first mismatching snapshot was overwritten by that wrapper's final
snapshot, so the exact transient value from the failure is not preserved.

The wrapper now preserves its first post-initialization snapshot, compares
stable clock/reset/TDI registers exactly, and checks DDR ready/normal-mode
fields using masks. A separate read-only verification audited the original
trace: all11 targets completed with code1, current prerequisites passed, and
protected clocks/resets/TDI matched the original snapshot. No initialization
was replayed and no DDR/TDI registers were written.

The original failed wrapper journal is retained as
`FLU-INIT-ATTEMPT-20260915.json`; the independent verification record is
`FLU-VERIFIED-20260915.json`. This distinction is intentional.

## Live session result

`validate_live_sessions.py` uses the locked, bounded native144-byte entry
adapter. It journals test keys before insertion and performs cleanup only
for entries whose full supported key/state matches its own records. It does
not bypass production packet qualification or change forwarding actions.

Test tuple: UDP198.18.0.1:40001 ↔198.18.0.2:40002, zone4094, flow IDs0x1001
and0x1002. Both preflight fetches returned native NOT_FOUND3. Insert and
post-insert fetch returned0 for both directions. Readbacks matched the exact
keys and flow IDs. Delete returned0 for both, and both final fetches returned3.
Cleanup completed with no errors; counters0x40428 and0x40450 returned zero.

`SESSION-TABLE-VALIDATION-20260915.json` contains all12 native operation
responses and the before/after state. `LIVE-SESSION-READY-20260915.json` is a
fresh read-only status with initialized=true and no session-table blockers.
`session_offload_verified` remains false because no packet lookup/offload
claim follows from a CPU table test.

## Validation and next work

36 targeted Python tests pass, including configuration reconstruction,
stale/failed training rejection, cfg4 target completion, recovery-journal
selection and session lifecycle tests. The stricter completion parser also
passed against the actual hardware trace. Commissioning, readiness and
validation scripts are deployed in CP `/usr/local/sbin`.

Remaining: broader memory/stress qualification, physical packet lookup
hit/miss checks, forwarding-action validation and policy invalidation on
the packet path. Production session offload remains gated until those pass.

CP boot identity: `c3b75f79-b285-4a5c-ad61-489d1038a298`. Raw logs on CP:
`/var/lib/ffn/fe100/flu-init-live.txt` and
`/var/lib/ffn/fe100/session-validation-live.txt`.
