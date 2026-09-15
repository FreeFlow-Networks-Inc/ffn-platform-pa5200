# FE100 flow-memory recovery and diagnostics

Update: the FDT1 training failure described below was resolved on September15.
See [the verified pattern-initialization correction](FDT-TRAINING-FIX-20260915.md).
The remaining text records the earlier failed state.

FE100 session offload remains unavailable. FHM0, FHM1 and FDT0 completed
native training in this CP boot. FDT1 fails READ_CENTERING with error `0x808`.
Controller readiness bits and subsequently cleared PHY error registers do not
override that failure. This does not establish whether its cause is hardware,
configuration, or the reconstructed initialization environment.

## References

Implementation was checked against the VM sysroot
`/mnt/clones/5220-sysroot1-full`, specifically:

- `opt/dpfs/usr/local/lib64/libpandp_cp.so.1.0`, SHA256
  `b57227a460144c8c2545fc2e268b31f475ef72ac2a6f1d457387ab46d842c3e9`.
  Relevant routines: `fe100_dram_init`, `dphy_init`, `umctl2_init`,
  `fe100_enable_dram_clks`, `fe100_dram_post_init`, `run_init_cal_algorithm`,
  `check_init_cal_status`, and `fe100_get_dimm_info`.
- `opt/dpfs/usr/share/pdt/fe100.py`, `ddr.eye`: DPHY group coordinates,
  six-bit eye-width conversion, and reference arrays. All four copied arrays
  compare exactly against the sysroot. Its PA-5260 sample references are
  comparative diagnostics, not PA-5220 qualification limits.

## Live results

The earlier post-init-only retry retained partial calibration. A single new
recovery therefore reset only FDT1's controller/PHY and ran the native full
bringup sequence. FDT0 and TDI protected status remained unchanged; the
register adapter reported zero scope faults. Shared PLLs were not retuned.
An exclusive per-boot journal prevents repeating this recovery automatically.

The native verbose trace reports successful returns for WRITE_LEVELING,
DQS_ALIGN, RDCLK_ALIGN, WRITE_CENTERING, INIT_COARSE_WRITE and COARSE_READ.
READ_CENTERING reports error `0x808` and returns failure, which propagates to
the full bringup routine. Later successful stages clear the global error
register; the failure is retained in the journal and parsed log summary.

Fresh PHY diagnostics show measured eyes in FDT1 groups0/1, but zero eye and
timing registers in groups2/3. Those groups are reported as unmeasured, not
good 64-unit eyes. FDT0 bits46/47 fall below the PDT comparative threshold.
The first128 SPD bytes read from FDT addresses0x53/0x52 match; this does not
prove memory health or complete DIMM identity. No SPD page writes were made.

FLU initialization remains incomplete and its memory interfaces remain held
in reset. No session insertion or live session acceleration was tested.
Previously verified front5/front13 raw and TAP forwarding is documented in
`../octeon/debian/FRONT-FORWARDING-20260914.md`; no packet-path changes or new
forwarding qualification occurred during this DDR investigation.

## Implemented controls and checks

- A diagnostic MMIO mode permits only DPHY read address/command writes.
  PHY writes, controller targets, reset writes and wrong-device access are
  rejected. Native reads and SPD collection have watchdog bounds.
- Session status exposes the selected calibration journals. Failed, started
  or malformed controller recovery records supersede older successful
  records, independent of filesystem timestamps.
- An offline log parser preserves per-algorithm failures and incomplete
  stages. Its results never assert memory or offload qualification.
- 27 targeted Python tests passed, including journal precedence, failed
  calibration with apparently ready CSRs, unmeasured-eye handling and
  native-session lifecycle tests. The strict host C MMIO scope test passed.
- Updated diagnostics and session controls were deployed on the CP. The
  live writable-endpoint rejection test passed with zero register writes.

Evidence: `FDT-CONTROLLER-RECOVERY-20260914.json`,
`FDT-CALIBRATION-STAGES-20260914.json`, `FDT-DIAGNOSTICS-20260914.json`,
`LIVE-SESSION-STATUS-20260914.json`, and `LIVE-SESSION-GATE-20260914.json`.
The raw native log remains on the CP at
`/var/lib/ffn/fe100/fdt-controller-recovery-live.txt`; its hash is recorded in
the stage summary. CP boot: `c3b75f79-b285-4a5c-ad61-489d1038a298`.

## Next activation requirements

Resolve FDT1 read-centering, with attention to the unmeasured upper groups,
using the sysroot's lane configuration, initialization pattern and Vref
sequence. Do not bypass the error or replay the same recovery. After all
channels train, complete memory read/write validation, audited FLU setup,
session insert/fetch/delete tests and physical packet hit/miss qualification.
Those steps remain outstanding.
