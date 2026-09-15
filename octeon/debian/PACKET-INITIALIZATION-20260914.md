# PA-5220 staged packet and external lookup initialization

Implemented and exercised on the PA-5220, 2026-09-14. This is the first
initialization stage, not a working PKI/SSO/PKO3 forwarding driver.

## PKI preparation on the Debian DP

`octeon/kctl/ffn_dp_packet_init.c` is a CN78XX-only kernel module. Loading it
does not program hardware. Its debugfs status path reads reset state first and
avoids all other PKI accesses while reset is busy, as the SDK register definition
requires. It reports PKI/PKO enables, SSO add-work configuration, and enabled FPA
pool count without invoking any allocator.

The explicit `prepare-pki` operation loads all 1,001 PKI microcode words from
the operator's configured kernel tree and verifies every word. It refuses active
PKI, reset-busy PKI, or enabled PKI/PKO. It preserves the old instruction image
and attempts checked restoration on mismatch. It neither allocates packet
memory nor enables packet admission, scheduling, parsing, or egress queues.

Build using `octeon/debian/build-dp-packet-init.sh`, with KDIR, CROSS_COMPILE,
and OUT pointing to an isolated module build. `ffn_dp_packet_init.py` wraps the
operation with the fabric lock and a completed-Debian-boot check.

Live result:

```json
{"pki_reset_busy":false,"pki_active":0,"pki_enabled":0,"pko_enabled":0,
 "sso_aw_cfg":"0x000000000000000e","enabled_fpa_pools":0,
 "pki_microcode_prepared":true,"microcode_words":1001,"ready":false}
```

The module compiled with `KCFLAGS=-Werror` for
`6.18.49-ffn-debian-dp-20260914+`, loaded on the actual DP, and passed complete
microcode readback. Current staged module: `/root/ffn_dp_packet_init.ko` on DP.
It is not automatically loaded or applied on boot.

## FE100 TCAM clock stage on the CP

`fe100/ffn_fe100_clocks.py` defaults to a read-only plan. `--apply-tcam` accepts
only the audited PA-5220 reset state and exact clock configuration. It uses the
existing native 32-bit little-endian MMIO shim, with a register allowlist confined
to the five TCAM PLL controls, PLL status, TDI reset/status, and two TCAM monitors.
The shared FE100 table lock covers the entire operation.

The owner's hash-pinned `fe100_pll_initialize_config(0, 2, cfg)` supplies the
84-byte configuration through reads only. FFN applies the TCAM overrides from
`pan_fe100_set_tdi_config`, then performs its own checked writes and bounded
polling. It does not invoke the owner's broad TDI initialization routine.

Verified target controls at 0xa05a0..0xa05b0:
`07bd4120 0000000a 00002400 000000f8 00000001`.

The sequence enables energy monitoring, programs the PLL while reset is
asserted, uses the owner's one-second assert/deassert delays, verifies lock,
releases the TCAM TWG reset, then releases the 1x/2x soft resets. The external
TCAM system/core resets remain asserted and TCAM GO remains zero. No external
table, capability, DDR PHY, or session entry is programmed.

Live result:

- PLL status `0xa05b4 = 1` (locked).
- Monitor registers `0xa0710 = 3`, `0xa0714 = 3` (enabled, energy detected).
- TDI init status `0xa01c0 = 0x03800000`: core, TCAM 1x, TCAM 2x clocks observed.
- TDI reset control `0xa0004 = 0x3464`: external TCAM held in reset, GO disabled.
- Repeated apply returned `changed:false` and performed no writes.

The initial test omitted monitor enable and timed out waiting for clock-status
bits despite PLL lock. All modified controls were read back after rollback to
the original reset state. Enabling the monitors resolved that observation issue.
Consequently, earlier statements treating all zero clock-status fields as proof
that the clocks were off were too strong. `ffn_fe100_lookup_health.py` now reports
disabled-monitor clocks as **unknown/null**, retaining raw status separately.
DDR monitors are still disabled; their clocks are not qualified.

## Local reference evidence

Owner library on VM:
`/mnt/clones/5220-sysroot1-full/opt/dpfs/usr/local/lib64/libpandp_cp.so.1.0`, SHA256
`b57227a460144c8c2545fc2e268b31f475ef72ac2a6f1d457387ab46d842c3e9`.

- `pan_fe100_set_tdi_config`, 0x102b7db0: TCAM config at offset 1408,
  PLL interface 2; divider overrides at 0x102b7e20..0x102b7e60;
  TWG and soft reset release at 0x102b7e6c..0x102b7ef0.
- `fe100_pll_init`, 0x102eb7e0: settings, reset assertion, delay, deassertion,
  delay, and lock poll. The FFN implementation checks each write separately.
- `fe100_pll_initialize_config`, 0x102e9f10; 84-byte ABI recovered from DWARF.
- Owner CSR map identifies monitor EN bit0, energy detect bit1, PLL lock bit0,
  and TDI clock-status bits24/25. Register names and observed reset values agree.
- Owner `/etc/cfgdb/dp/5200/fe100.cfgdb.xml`: usecase1, cfg_mode4, v4_v6_choice2.
- SDK `cvmx-pki.c:cvmx_pki_setup_clusters` and `cvmx-pki-cluster.h` define the
  instruction load. `cvmx-pki-defs.h` documents reset-busy access restrictions.
- `tools/ffn_elf_got_calls.py` resolves MIPS GOT calls for this local audit.
  Owner binaries and SDK microcode sources are not copied into this repository.

## Validation and next stages

29 staged Python tests passed on the actual Debian DP, including the new clock
sequence/refusal/rollback/idempotence tests, clock-monitor interpretation, and
packet-init preconditions. Clock tests use mock MMIO; the separate live results
above establish the hardware PLL and PKI preparation outcomes.

Still required before packet admission:

1. Linux-owned FPA3 DMA allocations, reserved pools/auras, and drain/teardown.
   The old debugfs allocator assumes bootmem is outside Linux RAM and has no
   teardown. That assumption has not been verified for this Debian boot.
2. SSO XAQ head/tail buffers and aura ownership before setting AW_CFG.RWEN.
   Current AW_CFG=0xe has RWEN clear. Do not enable it without backing memory.
3. PKO3 internal aura, queue topology and descriptor queues. The current kernel
   does not link its imported PKO3 sources because the SDK WQE API conflicts
   with the upstream WQE API; the new initializer does not conceal that gap.
4. PKI style/QPG wiring to the owned aura and SSO group, followed by an explicit
   admission step and physical packet tests.
5. FE100 DDR PLL/clock sequence and PHY/controller training, external TCAM
   reset/capability/table initialization, then bidirectional session tests.

The vendor DDR clock helper writes whole TDI reset words (0x35c1, 0x3581,
0x0581) and releases additional PHY domains. It must not be treated as a single
clock-enable bit or copied into this limited TCAM stage without the matching
DDR initialization audit. Session offload remains unverified.
# Later DMA/queue work

The subsequent hardware results are in [DMA-QUEUES-20260914.md](DMA-QUEUES-20260914.md).
The earlier observations below describe the preceding microcode/clock stage.
