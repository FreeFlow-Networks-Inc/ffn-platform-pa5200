# CE40 bring-up, recovered from DWARF

Everything here is read out of `libpandp_cp.so.1.0` (PAN-OS 11.2.4-h5, MIPS64 BE,
30 MB of `.debug_info`) on the PA-5220 sysroot clone. No vendor code is copied;
these are register offsets, call orders and constants — facts about the board.

## The answer

```c
CE40_ILKPHY_CFG  =  BAR0 + 0x28000        /* 32-bit, big-endian, no swap */
                    write 0x72
```

This is the step the CP board agent performs before anything else touches the
chip, and the step FFN has never performed:

```
Loading: libfpga.so...
Setting CE40_ILKPHY_CFG to 0x72.
libfpga.so done
```

## How it was found

The register name is **not** a string in the binary — only the log message is,
and `0x72` is baked into that literal, so the name is a compile-time constant.
The route was therefore: string → call site → disassembly.

1. `"Setting CE40_ILKPHY_CFG to 0x72.\n"` sits at file offset `0x7be250`.
   `.rodata` is VMA `0x107bd240` at file offset `0x7bd240`, a constant
   `0x10000000` delta, so the string is at **VMA `0x107be250`**.
2. No instruction references that address directly — this is MIPS n64 PIC, so
   it is built from `%got_page` + `%got_ofst`. The page is
   `(0x107be250 + 0x8000) & ~0xffff = 0x107c0000`, leaving an offset of
   `0x107be250 - 0x107c0000 = -0x1db0 = -7600`.
3. `daddiu <reg>,<reg>,-7600` has exactly five hits in 1.7 M lines of
   disassembly; two are `gp` prologue adjustments, and of the remaining three
   `addr2line` names one **`ce40_init` at `ce40_init.c:231`**.
4. The write is the next source line.

## The call site

```
ce40_init.c:231
    101716d8:  sltiu v0,v0,4
    101716dc:  beqz  v0,10171840          # -> the log call
    101716e0:  li    a3,4
ce40_init.c:232
    101716e4:  ld    t9,8632(gp)          # ce40_reg_wr
    101716e8:  lui   a1,0x2               ┐ a1 = 0x00028000
    101716f0:  ori   a1,a1,0x8000         ┘
    101716ec:  move  a0,s0                #  a0 = control block
    101716f4:  li    a2,114               #  a2 = 0x72
    101716f8:  jalr  t9
```

GOT slots were resolved by computing `gp` from the prologue
(`lui gp,0x93; daddu gp,gp,t9; daddiu gp,gp,6320` with `t9 = 0x10171590`
gives `gp = 0x10aa2e40`), then reading `.got` (VMA `0x10a9ae50`) and matching
against the symbol table.

## The access path, all the way to the store

```c
ce40_reg_wr(cb, off, val)
    -> pan_pcimap_wr32(cb, /*bar=*/0, off, val)      /* a1 = zero */

/* pan_pcimap.c:238-248, libpancommon_cp.so */
pan_pcimap_wr32(ctx, bar, off, val)
{
    if (bar < 0 || bar > *(int *)ctx)        return -EINVAL;
    e = ctx + (bar + 1) * 16;                /* {u64 base; u64 len;} */
    if (off > e->len)                        return -EINVAL;
    *(uint32_t *)(e->base + off) = val;      /* bare `sw` */
    return 0;
}
```

The store is a plain `sw` with **no byteswap**. On the big-endian CP that means
BAR0 is accessed natively big-endian — independently confirming what
[BAR0-MAP.md](BAR0-MAP.md) measured on CA1, and matching `ffn_nac`'s default
`be_mask=0x9`.

Bounds: `0x28000 < 0x40000`, so the register is inside BAR0's 256 KiB window.

## The whole of ce40_init(), in order

Source is `libs/dp/fpga/ce40/src/ce40_init.c`. Control block is **392 bytes**
(`memset(cb, 0, 392)` at :192).

| line | call | notes |
|---|---|---|
| 192 | `memset(cb, 0, 392)` | |
| 196 | `cb[380] = instance` | |
| 201 | devid = **`0xfeed_a00d`** | `lui 0xfeed; ori 0xa00d` — confirms CE40's PCI id |
| 204 | `pan_pcicfg_open(&cfg, instance)` | fail → `"pan_pcicfg_open() failed: instance=%d, rv=%d"` |
| 209 | `pan_pcimap_map_device(cb, &cfg)` | |
| 215 | `pan_pcicfg_close(&cfg)` | |
| 217 | `ce40_reg_rd(cb, 0x008, &v)` | |
| 219 | `cb[384] = v >> 30` | a 2-bit field — the DDR layout selector (`CE40_MDL_SMALL/MEDIUM/LARGE`, cf. `ce40_get_ddr_layout`) |
| 225 | `ce40_reg_rd(cb, 0x000, &v)` | version register |
| 226–228 | version check | warn `"ce40 version (%d) below minium of (%d)"` *(vendor's typo)* |
| **232** | **`ce40_reg_wr(cb, 0x28000, 0x72)`** | **CE40_ILKPHY_CFG** |
| 237 | `nac_capabilities(cb + 112)` | |

A separate branch (`:177`, taken when arg3 == 0) calls `nac_mem_check()` and on
failure logs `"CE40 memory check failed."` at :188.

Note the ordering: the ILKPHY write happens **after** PCI mapping and the
version read, but **before** `nac_capabilities`. It is not a reset-time poke.

## Related entry points in the same library

```
ce40_init             ce40_fini            ce40_get_cb
ce40_reg_rd           ce40_reg_wr          ce40_reg_rmw
ce40_bar_rd           ce40_bar_wr
ce40_clk_reset        ce40_interface_reset ce40_chk_ready
ce40_get_ddr_layout   ce40_get_cip_cnt     ce40_get_dfa_cnt   ce40_get_dp_count
ce40_ilk_set_lpbk     ce40_ilk_int_lpbk_test   ce40_ilk_pkt_gen_test
ce40_cip_dfa_tbl_write (static)   ce40_cip_dfa_tbl_dump (static)
```

`ce40_ilk_*` are the Interlaken loopback and packet-generator tests — the
obvious way to prove the link after the ILKPHY write lands.

## Status and the honest caveat

**Executed and wired (2026-10-07).** `ffn_ce40_init.py` performs the vendor's
order on every boot from `ffn-ce40-init.service` (deployed in the CP root,
ordered before the FE100 units): on the live CP it read fpga version 19,
DDR layout 2, RLD cal `0xe2`, and wrote `ILKPHY_CFG 0xeec -> 0x72`, verified by
read-back. The two gates below are therefore history, kept for the record:

1. **It is unconfirmed that `feed:a00d` enumerates on our board at all.** See
   [README.md](README.md). The CE40 is an FPGA and may only appear as a PCI
   endpoint once programmed.
2. CA1 (`feed:a101`) *is* present and shares this register map
   (`ce40.py` ≡ `ca1.py`), and `0x28000` is inside its BAR0 — so the offset can
   be **read** through `ffn_nac` on CA1 today as a sanity check. Do not read a
   value there as proof of anything about the CE40: CA1 has no Interlaken
   (`nac-ca1` is absent from PAN-OS 11.2 entirely), so an ILKPHY register may
   simply not be implemented in its bitstream.

Writing `0x72` requires `ffn_nac` loaded with `allow_write=1`, which is off by
default for good reason — this is the control plane of a live box.
