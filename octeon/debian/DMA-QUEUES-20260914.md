# PA-5220 DMA and queue commissioning — 2026-09-14

## Verified on the dataplane

Kernel: `6.18.49-ffn-debian-dp-20260914+`, CN78XX, Debian systemd.
Module SHA256: `365904adac5b0698548dc50a100fc3f06f52468854c270ed245cdf89f1f55ec5`.
The loaded module is `/root/ffn_dp_packet_init-dma.ko`. It is explicitly loaded
for commissioning; initialization is not installed as an automatic boot service.

| Purpose | Pool / aura | 4 KB buffers | Available after setup | Hardware-held |
|---|---|---:|---:|---:|
| Packets / WQE | 61 / 1021 | 512 | 512 | 0 |
| SSO XAQ | 62 / 1022 | 570 | 314 | 256 |
| PKO3 DQ / jump | 63 / 1023 | 1024 | 1004 | 20 |

All 2,106 buffers passed native FPA allocation, ownership, uniqueness,
exhaustion and return-count checks. Allocation uses Linux `dma_alloc_coherent`
and rejects nonidentity or unaligned DMA mappings. The module is pinned before
advertising addresses. Buffers and pointer stacks remain allocated until DP
reset once hardware can reach them, including on partial failure.

SSO: all 256 XAQ head/tail pointers and next pointers were programmed and read
back. IAQ/TAQ per-group thresholds and global reservation sums were verified.
RWEN remains clear because no packet consumer is commissioned.

PKO3: the dedicated FPA interface was enabled and `PKO_STATUS.PKO_RDY` asserted
within the bounded poll. It fetched 20 buffers. The single-child scheduling
hierarchy L1→L2→L3→L4→L5→DQ at queue zero was readback verified; its MAC12
mapping is CN78XX BGX2/LMAC0. DQ opening, channel credit mapping, MAC/FIFO
configuration and PKO enable remain separate, unfinished steps.

PKI: all 1001 microcode words were reloaded and readback verified after the DP
restart. PKI is disabled. Style/QPG/aura mapping and work consumption remain.

The initial commissioning attempt stopped on a range-register readback error
before any pool was enabled. CN78XX range addresses occupy bits 41:7, unlike
the old FPA1 encoding used in the earlier experimental allocator. The corrected
module passed after a DP-only restart. The built-in legacy `ffn_fpa3/setup`
interface was not used or fixed; do not use it concurrently with this owner.

The internal BGX2 40G link was restored after reboot: enabled, block lock,
link up, PKND8, no fault. Debian reported zero failed units. MP/CP were not
restarted. Repeating DMA and SSO preparation left pool counts unchanged.

## Explicit commands

After boot health is ready and the module is loaded, on the DP:

```sh
python3 /usr/local/sbin/ffn_dp_packet_init.py prepare-pki
python3 /usr/local/sbin/ffn_dp_packet_init.py prepare-dma
python3 /usr/local/sbin/ffn_dp_packet_init.py prepare-sso
python3 /usr/local/sbin/ffn_dp_packet_init.py prepare-pko-memory
python3 /usr/local/sbin/ffn_dp_packet_init.py prepare-pko-queues
python3 /usr/local/sbin/ffn_dp_packet_init.py status
```

The kernel accepts only the fixed plan, requires CAP_SYS_RAWIO and serializes
initialization. The CLI shares the fabric lock and issues exactly one unbuffered
write, so a hardware error cannot silently trigger a second command on close.
This is exclusive DP commissioning, not a shared SDK allocator. No SDK process
or legacy debugfs allocator may claim these resources concurrently.

## FE100 external table implementation

`fe100/ffn_fe100_external_tables.py` supplies the cfg4 IPv4/IPv6 initialization
transactions and the exact 64-byte native IA request ABI. The 263 transactions
were independently compared with the hash-pinned native owner's
`fe100_tdi_table_cfg4_v4_v6` routine on the CP. A test interposer captured every
IA call with no MMIO, EEPROM or table access. All addresses and data words matched.

The initializer requires an exclusive transport, verified DDR training and
memory testing, TCAM readiness and a quiescent lookup path. It tracks partial
completion and demands configuration verification before a completion mark.
**The production transport for this new initializer is not yet integrated;
these transactions were not applied to the external TCAM.** A transport test is
not a physical offload test.

Eight Python tests passed on the actual MIPS64 BE DP: preconditions, no retry
after a write failure, ABI layout, partial transaction failure, failed
verification and successful simulated completion without an offload claim.
The kernel module compiled with `KCFLAGS=-Werror`; `git diff --check` passed.

## DDR training remains unfinished

Owner reference: VM `/mnt/clones/5220-sysroot1-full/opt/dpfs/usr/local/lib64/libpandp_cp.so.1.0`,
SHA256 `b57227a460144c8c2545fc2e268b31f475ef72ac2a6f1d457387ab46d842c3e9`.
Resolved initialization calls:

- `fe100_dram_initialize_config(dev=0, type=2, cfg)` at `0x102dd948`.
- `fe100_dram_bringup_sequence(type=2, cfg)` at `0x102e8fc0`.
- `fe100_vref_training_sequence(type=2, dev=0, cfg)` at `0x102e9508`.
- `fe100_tdi_table_cfg(dev=0, mode=4, v4_v6=2)` at `0x102b7808`.

TDI DDR config is a 248-byte structure at offset1576 in the 2812-byte owner
configuration. Its initializer sets interface2 and consults EEPROM bus0,
address0x57, tuple0xf00c. A fresh 24-byte read at offset zero returned all FF.
The device-tree EEPROM node is on bus1; older I2C notes document that mismatch.
The owner's `fe100_get_dram_info` can return zero after EEPROM failure, retaining
defaults. Do not treat that return value as verified board geometry.

`tools/ffn_fe100_ddr_profile.py` extracts the hash-checked defaults offline.
Those raw defaults have uninitialized interface/PLL/training fields and must
not be sent directly to a PHY. The full runtime configuration and reset,
controller, PHY, calibration and Vref sequence still need validation, followed
by bounded training-status checks and memory tests. No DDR reset was released
and no training was attempted in this update. Existing TCAM PLL/clock readiness
remains verified; disabled DDR monitors still mean unknown clock state.

**Actual forwarding and FE100 session offload remain unverified and disabled.**
See `DMA-QUEUES-VALIDATION-20260914.json` for the captured native queue state.
