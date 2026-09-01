# ffn_bde: the SDK attaches. Verified contract, and what is left.

FFN's own `linux-user-bde` replacement. The vendor SDK shell now attaches the
BCM88375 through it and identifies the chip correctly:

    SPI unit 0: Dev 0x8375, Rev 0x11, Chip BCM88375_B0, Driver BCM88375_B0
    rc: unit 0 device BCM88375_B0

That is up from `open /dev/linux-user-bde: No such file or directory`, and it
independently confirms B0 — the same revision the driver struct's PCI field gave.

## Method: let the SDK tell you the ABI

The module implements the minimum and **logs every unhandled command with its
number and payload**. Each run gets further and names the next thing it wants, so
bcm.user's own diagnostics drive development.

That is also the safe order. An unimplemented command fails cleanly; a
wrongly-guessed register or DMA command writes to the wrong physical address on
live silicon.

The sequence it walked, each step discovered by running it:

    open succeeds                 -> asks command 5   (get_dma_info)
    DMA region reported           -> asks command 30  (get_dev_state)
    30 and 29 answered            -> mmap fails, EINVAL
    d1 flag corrected             -> "device 0x14e4 revision 0x75 not supported"
    identity fields corrected     -> "Unknow bus type 0x0 !!"
    bus type corrected            -> ATTACHED, then rc-script failure

## Verified contract

**Command 2, get_device.** Field order established empirically. Reporting vendor
in d0 produced `device 0x14e4 revision 0x75` — 0x14e4 is the vendor and 0x75 the
low byte of device 0x8375. So:

    d0 = PCI device id      (0x8375)
    d1 = PCI revision       (0x11)
    d2 = PCI vendor id      (available, but not what it keys on)

**Command 5, get_dma_info.** Contract read out of the client's own
`_get_dma_info`, which assembles a 64-bit address with `dsll32` then `or`:

    *a0 = (dx.dw[1] << 32) | dx.dw[0]     64-bit physical base
    *a1 = d0                              size
    *a2 = d1                              flag, see below

So dx.dw[0..1] carry the region's physical address low-then-high. A 4 MB coherent
allocation works, and `dma_handle == virt_to_phys()` on this platform — checked at
allocation rather than assumed, because userspace mmaps the physical address while
the device uses the handle, and a divergence would corrupt silently.

**d1 selects the mmap path.** With d1=0 the SDK called libc mmap on an fd that is
not ours, got EINVAL, and asserted at `linux-user-bde.c:1071` with our mmap
handler never entered. With **d1=1** the mmap succeeds and it reports
`DMA pool size: 1`. The client stores d1 into a global that `_mmap` later reads,
which is what pointed at it, together with the `_use_kernel_bde_mmap` symbol.

**Command 12, get_device_type, must return 2.** This is the bus type. 0 gives
`Error : Unknow bus type 0x0 !!`; 1 and 4 both fail; **2 attaches**. The SDK then
labels the unit "SPI", which is its own category name rather than a statement
about the physical bus — the device is plainly PCI.

**Command 29, instance_attach** sends d0 and d1 and reads nothing back.
**Command 30, get_dev_state** returns a state in d0; 0 is accepted.
**Command 21, bus_features** fills be_pio / be_packet / be_other.

## Deliberate uncertainties, exposed as module parameters

Values that are not established are parameters, not constants, so they can be
probed without a rebuild:

    dev_type=2      verified correct
    dma_flag=1      verified correct
    dma_mb=4        works; 2 also works
    dev_state=0     accepted, semantics unknown
    be_pio / be_packet / be_other = 1      NOT verified, see below

`be_*` deserves the caution. ffn_bcm already reads this device correctly and its
own note records BAR0 register 0 reading `0x75830000` where the device id is
`0x8375` — raw access returns the halves swapped and FFN swaps in software. So
telling the SDK that PIO needs swapping is evidence-based rather than a guess. But
a wrong value here does not fail loudly; it silently byte-swaps every register
access to a live switch. That is exactly why it is a runtime knob.

## Why it does not claim the PCI device

`ffn_bcm` already binds 14e4:8375 and is FFN's verified path to the CMIC. Two
drivers cannot claim one device, and taking it from ffn_bcm would cost the tooling
used to observe what bcm.user does — which is the point of running it. So ffn_bde
looks the device up with `pci_get_domain_bus_and_slot()` and never registers a
`pci_driver`. Both modules load together.

## Where it stops now

    ERROR: Assertion failed: (rm) at src/sal/core/unix/sync.c:554
    ERROR loading rc script on unit 0

The SAL sync layer fails to create a mutex. **Not memory**: 230 MB free before,
227 MB during, no OOM, no allocation failures in dmesg, `/dev/shm` present — and
memory barely moves, so it fails early rather than exhausting anything. A separate
investigation, and the next thing to chase.

Loading the rc script is where DNX init actually begins, so this is the last gate
before the vendor stack starts programming the switch.


---

## `sync.c:554` identified

The assert the SDK dies on is **`sal_mutex_take` checking that its handle is
non-NULL** — not a mutex that failed to be created.

Found without dumping a quarter-gigabyte line table: the assert macro passes
`__LINE__` as an argument, so compute the exact `jal` encoding for `_sal_assert`
(`0x0d1216a0`), scan `.text` for it to get all **407** calling functions, then
intersect with functions loading 554 (`0x22a`) into an argument register. Four
candidates; only one has it in `a2`, the line-number position.

Confirmed by disassembly at `0x14486758`:

    sd   a0, 32(s8)      ; save the mutex handle argument
    ld   v0, 8(s8)       ; rm = mutex
    bnez v0, +0x6c       ; if rm != NULL, proceed
    li   a2, 554         ; else line 554
    jal  _sal_assert     ; assert(rm)

So something calls `sal_mutex_take(NULL)` — a mutex that was never created,
because an earlier init step did not run or bailed silently. A failed
`sal_mutex_create` would return NULL gracefully; it is only a `malloc(48)` plus
`pthread_mutexattr_init`.

### Ruled out by experiment

  * **Memory.** `mem=8G` took the CP from 442 MB to 7.77 GB. Identical failure,
    ~48 MB used. The earlier prediction that this would fix it was wrong.
  * **sw_state sizing.** Defining `sw_state_max_size`, `stable_size` and
    `stable_location` does take effect — the "not defined" warning disappears —
    but the assertion still fires. Tried invented values and PAN's own runtime
    values from `runningConfig.soc` (`stable_size=250000000`,
    `stable_location=3`).

`config.bcm` is a **hardcoded absolute path** in the binary, so CWD and
environment variables cannot redirect it. Overrides go in via a bind mount of an
augmented copy, which leaves the owner's file byte-identical — sha256
`59e093faa92c9a48` verified before and after every run — and is undone by
`umount`.

### Narrowed to

Counting direct `jal` calls on the init path:

    soc_dpp_attach                     creates 7 mutexes, takes none
    soc_dpp_init and its five callees  create 0, take 0
    shr_sw_state_init                  create 0, take 0

No function on the init path creates a mutex; all seven come from
`soc_dpp_attach`. So the NULL handle is a mutex **attach** should have created,
taken later when the rc script runs `init soc` — consistent with attach returning
early or partially while the rc script proceeds anyway.

**Next step, bounded:** find which of attach's seven `sal_mutex_create` sites are
actually reached, and which subsystem's mutex is taken first during `init soc`.
Further config guessing is not the way; two rounds of it produced nothing.


## Which mutex: a per-hardware-table one

`soc_dpp_attach` has seven `sal_mutex_create` sites. Six are named singletons;
the seventh is a loop, and the seventh is the answer.

**The six singletons**, resolved by computing `gp` from the PIC prologue and
reading the GOT slot each site indexes:

    SchanWB   SOC_CONTROL   Counter   MIIM   SCHAN   FSCHAN

Each is followed by a NULL check branching to the common error exit, so any of
them failing would abort attach outright rather than let init proceed.

**The seventh** takes its name from `GOT[-14344]`, which resolves to the symbol
**`soc_mem_name`** — the SDK's array of *hardware table* names. Reading it
confirms: `AGER_EVENT`, `AGER_FLAGS`, `AGING_CTR_MEM`, `AGM_MONITOR_TABLE`,
`ALTERNATE_EMIRROR_BITMAP`, and so on.

So it is a loop creating **one mutex per hardware memory**, doubly gated:

    entry = soc_control[unit] + 0x1550698 -> +72 -> array[i]
    if (entry == NULL)        skip        ; beqz at 1250b12c
    if (!(entry->flags & 2))  skip        ; andi 0x2 / beqz at 1250b16c
    mutex[i] = sal_mutex_create(soc_mem_name[i])
    store into a per-index slot at +2528

That matches the timing exactly: the assert lands immediately after
`0: Init SOC.`, and `init soc` writes hardware tables — each access takes that
table's mutex. With the per-memory state array unpopulated, the loop creates
nothing and the first table access takes NULL.

It also explains why two rounds of config-property guessing achieved nothing. The
missing thing is not a size or a location, it is the per-memory state array that
gates mutex creation.

### Method note, because it generalises

Resolving a PIC name argument needs no debugger. Recover `gp` from the prologue
arithmetic — `gp = (lui_hi << 16) + entry + daddiu_lo`, giving `0x1a4d23b8` here —
read the GOT slot the site loads, and base plus the site's displacement is the
string. Same family as the `jal`-scan that located `sync.c:554`. Scripted as
`tools/ffn_gotstr.py`.

### CORRECTION: nothing populates it, and the above conclusion was wrong

The array is not a runtime structure. The chain resolves entirely to static
chip-driver data:

    soc_control[unit] + 0x1550698   ->  chip_driver   (soc_driver_t *)
    soc_driver_t      + 72 (0x48)   ->  mem_info      (soc_mem_info_t **)
    soc_mem_info_t    + 0x00        ->  flags, tested against bit 1

`soc_control_s` is 22,352,144 bytes with 405 members and DWARF names the member at
`0x1550698` as **`chip_driver`**. `soc_mem_info_t` is 112 bytes with `flags` at
+0x00, confirming the `lw v0,0(v0)` / `andi 0x2` reading.

So `chip_driver` is `soc_driver_bcm88375_b0` and `mem_info` is its **static**
array. Counted statically:

    non-NULL mem_info entries : 1262 of 10730
    entries with (flags & 2)  : 1262      <- every single one

Bit 1 is effectively "this memory exists on this chip", not an opt-in cache flag.
The loop should create 1262 mutexes, roughly 60 KB of mallocs. **Nothing is
missing and nothing needs populating**, so the conclusion above — that the missing
mutex was a per-table one whose gating array was empty — does not hold.

The follow-on idea, that attach returns early before reaching the loop at ~85%
through the function, is also wrong. Asked directly, the shell answers:

    Attach: Unit 0 (BCM88375_B0): attached (current unit)

### Where that leaves it

The specific NULL mutex is **still unidentified**. Eliminated with evidence so
far: memory total (442 MB -> 7.77 GB, no change), `sw_state`/`stable` properties
(warning clears, assert remains), an unpopulated per-memory array (it is static
and complete), and attach bailing early (the unit reports attached).

**Better approach than more static scanning:** the shell survives the assert and
answers queries — which makes it a live diagnostic tool this work had not been
using. Run `init soc` by hand, then its sub-steps individually, and bisect which
one asserts. That will localise it in a few commands, where cross-referencing
hundreds of `sal_mutex_take` sites statically will not.
