# The packet path after L1 — what blocks FPA, and three ways round it

**Status: L1 done, packet path not started. Measured 2026-09-05.**

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
