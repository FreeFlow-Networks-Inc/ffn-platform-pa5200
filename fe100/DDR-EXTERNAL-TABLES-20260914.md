# PA-5220 DDR and external-table commissioning

Update: the TCAM failure recorded below has been recovered. All263 entries pass
live readback and MP recovery status is clear. See `TCAM-RECOVERY-20260914.md`
and `TCAM-RECOVERED-20260914.json`. The failure details below are retained as
the pre-recovery investigation record.

## Verified on hardware

The sysroot's native `fe100_dram_initialize_config(0,2,cfg)` fills training
fields that are zero in the raw `fe100_cfg1` object. Missing EEPROM data has an
explicit native fallback. The runtime TDI profile was captured without MMIO,
then compared against the live initializer before training.

Owner library SHA256:
`b57227a460144c8c2545fc2e268b31f475ef72ac2a6f1d457387ab46d842c3e9`.
Reference: `/mnt/clones/5220-sysroot1-full/opt/dpfs/usr/local/lib64/libpandp_cp.so.1.0`.
The PDT reference is `opt/dpfs/usr/share/pdt/fe100.py`, DDR section around3544.
Its `fe100_dram_sa_bringup_sequence` entry is absent from this library; the
implemented path uses the exported configuration and bringup functions.

DDR PLL and clock initialization succeeded. Native
`fe100_dram_bringup_sequence(2,cfg)` returned0 with no denied register accesses.
Live DPHY status is `0x4d1`, reset control `0x4d81`, DDR PLL status1, and DDR
clock monitors3. These are live observations rather than cached success flags.

The external-memory TLU bridge uses IA registers `0x80500..0x80518`, block16,
target`0x2200`, address0. The old write/read helper allocates the block's maximum
width but initializes only four data words. FFN instead sends exactly four
initialized words using the native IA ABI. Write and read each completed with
CSR completion code1. Readback was `00aa5555 aaaa5555 aaaa5555 aaaa5555`, passing
the owner's low16-bit first-word/full subsequent-word comparison.

Measured eye widths:
`60 57 62 58 59 63 60 60 59 60 59 61 58 61 61 61`.
All pass the vendor PDT threshold (reference difference must exceed -12).
This is a diagnostic check, not exhaustive DDR stress qualification. No Vref
sweep or full-address memory test was completed.

## External table result: failed verification

The recovered cfg4 IPv4/IPv6 plan submitted263 writes. Native calls returned
success, but the first readback at address1 timed out with completion code0.
The journal records `stage=failed`, `completed=263`; that count does not mean
verified table contents. Native return0 is now also checked against IA cc1.

Subsequent health samples show one pending TLU external TCAM request and five
entries in each TDI external response FIFO. No further IA commands or automatic
reset/reinitialization were issued. DDR remains trained. Session activation is
blocked. Preserve the failed journal before developing a bounded recovery.

CP evidence:

- `/var/lib/ffn/fe100/ddr-training-live.txt`
- `/var/lib/ffn/fe100/ddr-memory-verified.json`
- `/var/lib/ffn/fe100/external-tables.json`
- `/var/lib/ffn/fe100/clock-init-1789412599942329079.txt`

Repository evidence: `HARDWARE-STATUS-20260914.json` contains a live MP collection
from both planes. DP wall time differs from CP; use per-plane boot identity,
not cross-plane wall-clock equality, when correlating this capture.

## Remaining work

Trace TCAM response synchronization and the owner's finalization sequence.
`pan_fe100_set_tdi_config` at `0x102b86a8..0x102b8748` performs an additional
target`0x200`, access-type4 command (`0x08000401`) after table setup. That command
is deliberately outside the current adapter allowlist; its bulk effects and
completion/recovery need auditing before use. Do not replay it over a pending
IA request. Do not treat write submission as external-table activation.

After recovery: repeat configuration/readback, complete external memory/lookup
tests, implement the native144-byte session-entry adapter, and test insert,
lookup hit/miss, forwarding, removal, and policy invalidation on physical ports.
The existing64-byte FFN flow wire record is not that native C structure.

DP has verified DMA pools, SSO memory and PKO queue topology, but PKI input
mapping/consumers and PKO MAC credits/transmit remain incomplete. PKI/PKO are
disabled and the queue remains closed. Physical forwarding is not qualified.

## Control scaffolding and validation

`management/hardware_harness.py` provides optional factory-gated CP/DP adapters,
live status, boot-ID-bound DP preparation, prerequisite and postcondition
checks, admin-only API routes and audit hooks. Non-PA platforms do not construct
adapters. `ffn_fe100_hardware_status.py` reports live clock/training status and
historical memory evidence separately from unavailable offload capability.

Deployed harness: MP `/opt/ffn-platforms/pa5200-hardware`.
Live CLI status succeeded; `prepare-dma` returned `changed=false, verified=true`
without reallocating existing pools. The API router is tested but not mounted
in the production WebUI. No core FFN files or running WebUI were changed.

Validation:25 Python tests pass (13 clock/table/runtime,9 orchestration,3 API),
plus the host C allowlist regression. Native MIPS adapter compiled with
`-Wall -Wextra -Werror`. HTTP client test dependencies were installed in an
isolated `test-deps` directory, not the production Python environment.

No commit or GitHub push was performed in this work.
