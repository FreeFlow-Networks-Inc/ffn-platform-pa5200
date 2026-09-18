# FDT1 training correction — verified on PA-5220

FDT1 training now succeeds. FFN enables the missing INIT_PAT_WRITE stage
before read-centering for the observed DDR4 configuration. All eight native
training algorithms returned success, and all four FDT1 PHY groups now have
measured eyes. Session acceleration remains unqualified.

## Sysroot evidence

Reference: VM `/mnt/clones/5220-sysroot1-full/opt/dpfs/usr/local/lib64/libpandp_cp.so.1.0`,
SHA256 `b57227a460144c8c2545fc2e268b31f475ef72ac2a6f1d457387ab46d842c3e9`.
No timing values, lane masks, voltage settings, or shared PLL settings were
changed to obtain this result.

- `fe100_dram_initialize_config` writes1 to FDT0's configuration offset188
  at instruction `0x102ddaa8`. Its FDT1 branch leaves the corresponding field
  at the zero value in the exported `fe100_cfg1` template.
- `enable_init_cal_one_by_one` at `0x102e604c` checks offset188 and branches
  to the INIT_PAT_WRITE call at `0x102e6168` only when enabled. It runs before
  DQS_ALIGN, RDCLK_ALIGN and READ_CENTERING.
- The string at `0x10883558` identifies the algorithm as INIT_PAT_WRITE.
- The prior live configurations have matching DDR type/width/DIMM enum values
  `(1,1,1)`. The successful FDT0 configuration has offset188=1; failed FDT1
  has offset188=0. Its prior verbose trace omitted INIT_PAT_WRITE and reported
  READ_CENTERING error `0x808`.

FFN's correction sets only the four-byte field at full configuration offset
1152 (`964+188`). It rejects other profiles or disabled required calibration
stages. It is applied after the native initializer for future FDT1 training.
Native calibration checks remain enabled.

## Recovery and validation

A separate `--recover-init-pattern` operation requires this boot's failed
controller-recovery journal, empty tables, FLU memory interfaces in reset,
and the previously audited clock/reset state. It records an exclusive journal
before hardware changes and resets only FDT1's controller/PHY. The existing
native full bringup then executes with the corrected flag. This recovery
returned0 with zero denied register accesses. FDT0 controller status and TDI
external-lookup status were checked unchanged.

Observed native stages, all returning success:
WRITE_LEVELING, INIT_PAT_WRITE, DQS_ALIGN, RDCLK_ALIGN, READ_CENTERING,
WRITE_CENTERING, INIT_COARSE_WRITE, COARSE_READ.

Fresh diagnostics show FDT1 eye widths50–64 across64 data bits, with all four
groups measured and no bits below the sysroot PDT comparative threshold.
The threshold uses a PA-5260 reference sample, so this is not exhaustive
PA-5220 memory qualification. FDT0's eye measurements remain unchanged,
including its previously recorded comparative outliers at bits46/47.

The selected FDT1 calibration journal is now `completed`; session status no
longer reports failed calibration. It still reports three FLU blockers:
incomplete memory initialization, usecase not configured, and memory
interfaces in reset. Live session activation was rejected with zero register
writes. No session entries were inserted and no packet-offload result is
claimed.

30 targeted Python tests passed, including the single-field correction,
unsupported-profile rejection, recovery-journal precedence and separate FLU
activation gate. Updated commissioning and session controls are deployed at
CP `/usr/local/sbin`. No native binary change was needed for this fix.

## Evidence

- `FDT-PATTERN-RECOVERY-20260915.json`: completed journal, exact correction,
  configuration, CSR snapshots and zero scope faults.
- `FDT-CALIBRATION-STAGES-20260915.json`: all stage outcomes and raw-log hash.
- `FDT-DIAGNOSTICS-20260915.json`: measured eyes, SPD and unchanged CSR snapshot.
- `LIVE-SESSION-STATUS-20260915.json`: calibration journal selection and FLU blockers.
- `LIVE-SESSION-GATE-20260915.json`: activation rejection with zero writes.

CP boot identity: `c3b75f79-b285-4a5c-ad61-489d1038a298`. Raw log:
`/var/lib/ffn/fe100/fdt-init-pattern-recovery-live.txt`.

Remaining work is memory read/write qualification, audited FLU initialization,
and native session/physical lookup validation. Training success alone does
not demonstrate session offload.
