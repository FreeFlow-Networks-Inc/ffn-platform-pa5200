# The packet path after L1 — what blocks FPA, and three ways round it

**Status 2026-09-06: FPA3 (own code) and PKI + SSO (imported) work. PKO3 is
blocked, measured twice.**

The 40G dataplane link is up ([[BGX]] — `xl24` up, `rcv_lnk=1`, `blk_lock=1`).
Nothing can cross it: the dataplane has no FPA pools, no PKI (receive) and no
PKO3 (transmit), so both ends' counters are correctly zero.

The obvious next step — import the SDK's FPA sources the way BGX was imported —
runs straight into a foundation-header conflict. This records the measurement so
nobody repeats it.

## The measurement: swapping cvmx-fpa.h costs 996 errors

`arch/mips/include/asm/octeon/cvmx-fpa.h` in this tree is **upstream's**, and it
is the OCTEON I/II FPA1-only API. The SDK's file of the same name is the unified
FPA1/FPA3 dispatcher. They are the same size (~285 lines) and share almost
nothing: a diff is 388 changed lines. It is a generation replacement, not drift.

`cvmx-fpa.c` — which is where FPA3 lives — is written against the SDK's header.
Copying that header in and building:

```
996 errors
    218  request for member 'u64' in something not a structure or union
     91  request for member 's' in ...
     84  request for member 'cn78xx' in ...
     46  unknown type name 'cvmx_addr_t'
     35  expected specifier-qualifier-list before 'CVMX_BITFIELD_FIELD'
     28  'CVMX_OCT_DID_FPA' undeclared
```

The SDK header pulls in `cvmx-fpa-defs.h`, `cvmx-fpa1.h`, `cvmx-fpa3.h`,
`cvmx-scratch.h` and `cvmx.h`, and those want the SDK's `cvmx-address.h`
(`cvmx_addr_t`, `CVMX_BITFIELD_FIELD`) and `cvmx-oct-did.h`. Pulling those in is
the documented failure path — it is exactly what produced the oscillating
129→1→281→218→171→61→139 the first time anyone tried a partial generation swap
here.

**Reverted, and the tree builds clean again (0 errors, 0 undefined).** The revert
was free because the port is committed; before this session the same experiment
would have risked the whole forward port.

## Why it cannot simply be swapped

Both generations have live consumers *in the same kernel*:

| consumer | generation | note |
|---|---|---|
| `cvmx-cmd-queue.c` | old | upstream, built |
| `cvmx-helper.c` | old | upstream foundation, built |
| `cvmx-helper-util.c` | old | upstream foundation, built |
| `cvmx-fpa.c` (not yet imported) | new | where FPA3 lives |

The three old consumers use only seven symbols: `cvmx_fpa_alloc`,
`cvmx_fpa_free`, `cvmx_fpa_ctl_status`, `CVMX_FPA_CTL_STATUS`,
`CVMX_FPA_PACKET_POOL`, `CVMX_FPA_PACKET_POOL_SIZE`, `CVMX_FPA_WQE_POOL`. The
SDK header still provides `cvmx_fpa_alloc` / `cvmx_fpa_free`; the pool constants
it does not, because in the SDK those are configuration (`cvmx-config.h`), not
API.

So the *consumer* side is a three-constant problem. The cost is entirely in the
SDK header's own include closure.

## Where FPA3 setup actually lives

The already-imported `cvmx-fpa3.h` has the runtime operations inline —
`cvmx_fpa3_alloc`, `free`, `aura_to_pool`, `reserve_aura`, `release_pool`,
`pool_is_enabled`, and about twenty more. What it does **not** have is setup:

```
cvmx-fpa.c:1013  cvmx_fpa3_setup_fill_pool()
cvmx-fpa.c:1184  cvmx_fpa3_setup_aura_and_pool()
```

Roughly 350 lines, and they are the only part of `cvmx-fpa.c` the packet path
needs at bring-up. The naming and shutdown helpers around them are not required.

## Three routes, none free

1. **Own-code the FPA3 setup** (~350 lines) against the already-present
   `cvmx-fpa3.h` + `cvmx-fpa-defs.h`, both of which compile in this tree today.
   No header conflict at all, and it fits the own-code goal. Cost: it is silicon
   setup — aura/pool geometry, buffer fill, RED parameters — and getting it
   subtly wrong yields a pool that allocates and then corrupts under load rather
   than failing at init.

2. **Coherent generation import** — bring in the SDK's `cvmx-address.h`,
   `cvmx-oct-did.h` and the rest of the closure, and move the three old
   consumers with them. This is what the tree's own notes describe as the
   correct approach in principle, and it is also the one that has failed twice
   when attempted piecemeal. It would need doing in one pass, not iteratively.

3. **Excerpt-import only the two setup functions** from `cvmx-fpa.c` into an
   FFN-owned file, carrying the BSD-3 notice. Smallest diff, but it breaks the
   rule that imported SDK files stay byte-identical, so a future SDK re-import
   stops being a plain copy.

**Recommendation: (1).** The blast radius is zero, it needs no header surgery,
and the FPA3 register interface is small and well documented in
`cvmx-fpa-defs.h`. Validate it by allocating and freeing every buffer in a pool
and checking `cvmx_fpa3_get_available()` returns to its starting value — a fill
bug shows up there immediately rather than under traffic.

## Before any of that: prove the link carries frames

Worth doing first, because it is cheap and it de-risks everything above. Nothing
transmits at either end today, so all counters read zero and "the link is up" is
still only a PCS statement.

`BGX(2)_CMR(0)_RX_STAT6` (dropped) is the tell: with no FPA pool, a frame that
arrives has nowhere to go and is dropped, so **a rising drop count with a zero
receive count would prove traffic crosses the link** — before any of FPA, PKI or
PKO3 exists. It needs only a forwarding decision on the BCM toward port 24,
which this platform has done before (see `bcm/` notes on
`tm_port_header_type=ETH`).

---

# Update 2026-09-06: PKI and SSO in, PKO3 out

## PKI imported cleanly — and the reason is worth knowing

`cvmx-pki.h` needs only `cvmx.h`, `cvmx-pki-defs.h`, `cvmx-fpa3.h` and the two
helper headers, **and does not include `cvmx-wqe.h`**. So there is no
API-header generation conflict and the ordinary import pattern applies. 5 errors
to 0. Same for `cvmx-sso-resources.c`, which needed nothing at all.

46 `cvmx_pki` symbols and 5 `cvmx_sso` symbols in vmlinux, and — the part that
matters — **the code runs**: `PKI_SFT_RST[BUSY]=0`, and the style and QPG
allocators allocate/free/reallocate to the same index. See `ffn_pki.c`.

Two spellings went into the compat header: `CVMX_BITFIELD_FIELD` (the
endian-aware bitfield macro upstream never had), and `enum cvmx_pki_layer_type`
with its value macros, verbatim from SDK `cvmx-wqe.h:129-181`.

## A generation name collision, resolved

PKO3's registers live only in the SDK's `cvmx-pko-defs.h`, so that was swapped
(allowed — `*-defs.h` carry no linkage). It broke upstream `cvmx-pko.c`: **both
generations use the identifier `cvmx_pko_status_t`**, upstream for a return-code
*enum*, the SDK for a register *union* — and `cvmx-pko3.c` needs the union while
`cvmx-pko.c` needs the enum. The legacy enum was renamed to
`cvmx_pko_legacy_status_t`, because it is dead on a CN78XX and its whole user
set is six lines of OCTEON I/II code here.

## PKO3 is blocked on cvmx-wqe.h, and this was measured twice

PKO3 needs the OCTEON III work-queue-entry types — `cvmx_wqe_78xx_t`,
`cvmx_buf_ptr_pki_t`, `cvmx_pki_wqe_word2_t`. They are **not separable**: they
are woven through the whole 1912-line SDK `cvmx-wqe.h`, which also redefines
`cvmx_wqe_t` itself. Lifting them the way the layer-type enum was lifted is not
possible.

| attempt | result |
|---|---|
| swap `cvmx-wqe.h` alone | 407 errors. Looked encouraging — all from 3 headers, one apparent failing TU. Adding the compat include there let the build get *further* and the count rose to **762** across five more upstream files. The first number was small only because the build stopped early. |
| swap it **plus** compat includes in all 20 upstream executive sources, one pass | **441 errors**, still dominated (106x) by `OCTEON_FEATURE_CN78XX_WQE` — *a compat spelling, in files that now include the compat header* |

That second result is the structural answer. Upstream sources include
`<asm/octeon/octeon.h>`, which drags in `cvmx-wqe.h` **before** the compat header
can be placed — and the compat header must come *after* `cvmx.h`. **There is no
include order that satisfies both.** The closure then widens further
(`cvmx_pow_tag_type_t`, `struct cvmx_wqe`) into `cvmx-pow.h`.

Both attempts reverted; tree builds clean, 0 errors, 0 undefined. The PKO3
sources are imported and left out of `obj-y`.

### What is left for PKO3

**Own-code the descriptor path**, as was done for FPA3. That is now the
recommended route rather than one of three: the generation import has been tried
properly, in one pass, and the obstacle is structural rather than a matter of
effort. PKO3's send path is a descriptor format plus LMTDMA/IOBDMA stores
against `cvmx-pko-defs.h`, which is already the SDK's in this tree.

## Where the packet path stands

| piece | state |
|---|---|
| FPA3 buffers | **own code, works on silicon** (selftest passes) |
| PKI receive classification | **imported, links, runs** |
| SSO | **imported, links** |
| PKO3 transmit | **blocked** — own-code it |

Receive is nearly assembled. Transmit is the gap.

