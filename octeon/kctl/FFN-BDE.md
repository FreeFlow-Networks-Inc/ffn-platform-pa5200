# ffn_bde: the SDK initialises through it. Contract, and what is left.

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

---

## RESOLVED: the mutex, and everything it was hiding

The bisect worked, but not by stepping `init soc`. Asking the SDK shell `soc`
produced the decisive line:

    CM: Base=(nil)
    SchanOps=0

The SDK had **no register window at all**. Every failure above was downstream of
that, and the NULL mutex was a symptom, not a bug of its own.

The cause was a single wrong module parameter, and the client's own `_open`
says so plainly. At `linux-user-bde.c:899`:

```
lw   v1,16(s8)        ; dev_type as reported by ioctl 12
lui  v0,0x1
ori  v0,v0,0x8d       ; mask = 0x0001008D
and  v0,v1,v0
beqz v0,104f025c      ; no bits match -> skip lines 903..961
```

`dev_type` was **2**. `2 & 0x0001008D == 0`, so the branch was taken and the
client skipped, in one jump:

* the register-window `_mmap` at line 935, hence `CM: Base=(nil)`;
* the `sal_mutex_create` at line 955, hence `sal_mutex_take(NULL)` at
  `sync.c:554` much later, nowhere near the cause.

Bit 0 is PCI. The banner had been saying so all along -- **`SPI unit 0`**, not
`PCI unit 0`; 2 is `BDE_SPI_DEV_TYPE`. The earlier note in this file claiming
"2 is CORRECT and verified" was wrong: it verified that the device *attaches*,
which it does either way, and mistook that for the device *working*.

### The `_open` call sequence, from the listing

Worth keeping, because the order is the contract:

| line | call | note |
|---|---|---|
| 818, 831 | `_ioctl` | asserted at 821, 834 |
| 850 | `_get_dev_state` | |
| 873, 891 | `_ioctl` | asserted at 876, 894 |
| 899 | *the gate* | `dev_type & 0x0001008D` |
| 903-928 | window size | bits 30/29/31 = 128K/256K/320K, default 64K |
| **935** | `_mmap` | the register window -> `CM: Base` |
| **941** | `_ioctl` 26 | `bnez` on the return -> skips 944..961 |
| **955** | `sal_mutex_create` | returns -1 at 956 if NULL |
| 960 | `if (base == 0) goto done` | a zero base cleanly skips the rest |
| **961** | `_mmap` | second window -> `vbase1` |
| 962 | `if (dev_type & bit25) goto done` | |
| 965 | `lw` through `vbase1` | BAR0+0x2C00; **bus errors on this board** |
| 1070-1073 | `mpool_init`, `_mmap`, `mpool_create` | the DMA region |

### Corrections to the contract recorded earlier in this file

**Command 5 `d1` is the mmap length, not a flag.** With it zero the SDK printed
`DMA pool size: 1` and mmapped exactly one byte. It is the size, same as `d0`.

**Command 2 `d3:d2` is the register window physical address.** Reported as
BAR2's base. It had been treated as spare, which is the direct reason
`CM: Base` was nil even after the window branch was reachable.

**Command 26 must return `rc = 0`.** The client does
`if (_ioctl(...) != 0) goto done`, and the code it skips contains the
`sal_mutex_create`. Failing this command is what produces the NULL mutex. In:
`d0` = resource index, sent as 1. Out: `d3:d2` = that resource's 64-bit
physical base. Resource 1 is **BAR0**: the client sizes that window at `0x8000`
when `dev_type` bit 29 is set, and BAR0 on this device is exactly `0x8000`.

**Never zero the input fields.** An earlier version satisfied the "reply with
all 96 bytes, unfilled fields zeroed" rule by wiping the struct in place. That
destroyed the arguments of every command that sends any: command 26 arrived with
`d0 = 1` and was dispatched as `d0 = 0`, so it answered "no such resource", the
client left `vbase1` NULL and then dereferenced it. The module now keeps the
received copy in `in` and builds the reply in `io`, and never mixes them.

**Command 21 is never called.** A full run issues only 5, 0, 1, 30, 12, 2, 26
and then works entirely through the mmapped windows. So `be_pio`/`be_packet`/
`be_other` are dead knobs -- changing `be_pio` between 0 and 1 produced
byte-identical failures. Byte order is **not** negotiated through the BDE; see
`paxb-byte-order.md` in the interop repo for how it actually works.

**`dev_type` bit 29 is not a guess.** The vendor's own `_pci_probe` does
`dev_type |= 0x20000000` for this device, giving the 256 KB register window.

### Where it stops now

With `dev_type = 0x22000001`, command 26 answered, inputs preserved, and PAXB
big-endian mode enabled at probe, the unit reports

    PCI unit 0: Dev 0x8375, Rev 0x11, Chip BCM88375_B0, Driver BCM88375_B0
    Flags=0x203: attached initialized
    CM: Base=0xfff0d27000

and `init soc` runs the real `jer.soc` script through our BDE:

```
+ 0: Read SOC property Configuration
+ 0: Device Reset and Access Enable
+ 0: Blocks OOR and PLL configuration
+ 0: Traffic Disable
+ 0: Blocks Initial configuration
```

The remaining failure at that point was the BAR0+0x2C00 read at line 965, which
took a data bus error and, in doing so, wedged the whole PCIe path and then the
appliance -- see `pcie-abort-hazard.md`. `dev_type` bit 25 makes the client skip
that read, and is on by default for that reason. **This combination has not yet
been run**: the appliance went down before it could be, and the module built
clean but untested on hardware.

### Next, in order

1. Bring the MP back up (needs a power cycle).
2. Read BAR0+0x2C00 once, on its own, before anything else touches the device --
   that settles whether bit 25 is a workaround or the correct answer.
3. Load `ffn_bde` (defaults are now right) and run `init soc` to the next stop.
4. Implement commands as the SDK asks for them: 3/4 PCI config, 23/24 cpu
   read/write, 27/28 iProc, 19/20 EB, 6/7/9/22 interrupts. None have been
   requested yet.

## RESOLVED 2026-09-01: the SDK runs DNX init to completion through this BDE

Broadcom's OpenBCM release (`github.com/Broadcom-Network-Switching-Software/OpenBCM`,
`sdk-6.5.26-DNX.1`) contains the source of the client this module serves --
`systems/bde/linux/user/linux-user-bde.c` -- and of the kernel module it replaces,
`systems/bde/linux/user/kernel/linux-user-bde.c`. The BDE is GPLv2 there. Everything below was
inferred from disassembly first and then confirmed or corrected against that source.

### Corrections to this file

* **Command 5 `d0` is NOT the size.** `_get_dma_info(&_cpu_pbase, &_dma_pbase, &_dma_size)`:
  `dx.dw[1]:dw[0]` = `_cpu_pbase` (what the client mmaps), **`d3:d0` = `_dma_pbase` (what the
  DEVICE targets)**, `d1` = size. `_l2p()` computes every DMA address as
  `_dma_pbase + (laddr - _dma_vbase)`. Reporting the size in `d0` sent the chip's first SBUSDMA to
  host address `0x00400080` -- measured in `CMIC_CMC0_SBUSDMA_CH1_HOSTMEM_START_ADDRESS`.
* **`dev_type` bit 25 is `BDE_NO_IPROC`** ("device uses two BARs, but is not iProc"), not "skip the
  PAXB probe". Besides skipping the `_open` loop at line 965, it makes `_iproc_read` bypass
  `_iproc_offset()` and index the 32 KB BAR0 mapping with a raw iProc address -- the SIGSEGV at
  `linux-user-bde.c:1872`. The loop it skips is IMAP window *discovery*: `BAR0+0x2C00..0x2C1C` are
  the `PAXB_IMAP0_n` registers, already programmed by firmware, and they read fine (probed with
  `get_dbe()`, then read by the client itself). Default is now `0x20000001`.
* **Command 26 `d1` is the resource size**, and rsrc 0 is the CMIC window (BAR2 here), rsrc 1 the
  iProc window (BAR0): `lkbde_get_dev_resource(dev, d0, &d2, &d3, &d1)`.
* **Command 22 writes a CMIC interrupt-mask register**: `lkbde_irq_mask_set(dev, addr=d0, mask=d1,
  fmask=0)` is a plain 32-bit store into the register window at offset `d0`.

### The five things that stood between "attached" and a completed init

1. **Bit 25 cleared** (above).
2. **`pci_set_master()`** -- never called, and nothing else does it: the CP boot line enables
   memory space only (`PCI_COMMAND 0x0142`). Without it the first SBUSDMA never completes and its
   status poll becomes a PCIe completion timeout: the `bcmINTR` / main-thread Data Bus Error on
   `CMIC_CMC0_SBUSDMA_CH1_STATUS`, a register that reads fine.
3. **Command 9 blocks.** `_interrupt_thread` loops `_ioctl(LUBDE_WAIT_FOR_INTERRUPT); _run_intr_handlers()`;
   an immediate error return made that a spin on status registers. Now MSI + a wait queue, with the
   ioctl mutex dropped across the sleep and the sequence counter sampled before re-arming.
   Commands 6/7 arm and mask; 22 does the mask write above.
4. **Command 5 `d0`** (above).
5. **The PAXB outbound window.** `shbde_iproc_paxb_init` names what the "PAXB steps 2-5" were:
   `PAXB_IMAP0_2` (0x2C08, bit 12 set = PAXB_1, `dma_hi_bits = pci_num ? 2 : 1`),
   `PAXB_PCIE_EP_AXI_CONFIG` (0x2104) = 0, **`PAXB_OARR_2` (0x2D60) = 1, `PAXB_OARR_2_UPPER` (0x2D64)
   = `dma_hi_bits`** -- the window through which the CMIC's DMA engines reach host memory -- plus
   `OARR_FUNC0_MSI_PAGE` (0x2D34) |= 1 and `CMICD_TO_PCIE_INTR_EN` (0x2380) bit 0 clear for MSI.
   This board reads `IMAP0_2 = 0x18012001`, so `dma_hi_bits = 1`. With it unprogrammed (or, from an
   inverted derivation, 2) every SBUSDMA reports `DONE` with no error bits and nothing crosses the
   bus: `dump raw IRR_MCDB 0 20` through the diag shell read all zero after the block-access check
   had written `0xaaff5500+i` there. With it right the check passes.

Also: the DMA region is mapped **cached** in userspace (only BARs uncached). PEM1 has
`bar2_cax = 0`, so the chip's inbound writes go to L2; the kernel's coherent mapping is cached
XKPHYS; the vendor maps the pool cached on every non-ARM arch. An uncached view reads around the
data -- which is also why a `/dev/mem` pattern test gave the wrong answer here.

### Where it stops now

    0: Init SOC Done.          (DDR tuning on all 8 DRCs, ports, scheduler, ITM, PP)
    0: Init BCM.  multicast qos stk l2 port stg mpls vswitch vlan cosq fabric linkscan
    + 0: rx
    bcm_petra_rx_init:1923 Out of memory

Not heap (7 GB free) -- the **4 MB DMA pool**. `bcm_rx_pool_setup()` allocates
`BCM_RX_POOL_COUNT_DEFAULT` packets of `RX_PKT_SIZE_DFLT` bytes as one DMA block, on top of what
table DMA already took. `dma_alloc_coherent` cannot exceed 4 MB on this kernel
(`CONFIG_FORCE_MAX_ZONEORDER=11`, no CMA). The vendor's `sbin/rc` loads its BDE with `himem=Y`:
a pool carved from memory the kernel does not manage. FFN's kernel has that facility already
(`ffn_reserve=`), so: `ffn_reserve=0x30000000,64M` on the CP boot line and
`ffn_bde dma_phys=0x30000000 dma_mb=64`. The module maps it through the XKPHYS direct map and refuses
any range that is System RAM (`page_is_ram()` -- `pfn_valid()` is section-granular under SPARSEMEM
and wrongly says yes -- the first attempt refused the reserve for exactly that reason).

**Result: the whole init completes.** With the 64 MB pool, `rx` passes and `Init BCM` runs on through
stat, l3, ipmc, field, policer, failover, mirror, time, ptp, tx, trunk to `Init BCM Done.` and
`jer.soc: Done.` (1597-line run, exit 0, running config saved). The RX pool is
`BCM_RX_POOL_COUNT_DEFAULT = 960` x `RX_PKT_SIZE_DFLT = 16 KB` on DNX builds -- 15 MB as one block,
which is why 4 MB could never have worked.

### Tooling

`get_dbe()` makes a probe of an uncertain offset a log line rather than a downed CP (MIPS
`__dbe_table`; `arch/mips/kernel/module.c` registers a module's table). Parameters: `probe_paxb_win`,
`probe_bar2` (with controls), `probe_bar2_off/_n`, `probe_bar0_off/_n`, `probe_pem`, and `dma_probe`
(sysfs `fill`/`show` of the pool through the coherent mapping). The harness
`tools/ffn-bcm-sdk.sh` is idempotent and refuses to insmod onto a PCIe path a Data Bus Error has
degraded -- `systemctl restart ffn-octeon.service` is the (software) recovery. The diag shell keeps
reading stdin after `jer.soc` fails, so `getreg` / `listmem` / `dump raw` can be driven from the
command file.


## Our own bcm.user: classified PCI, past CPS reset, stopped at a DMA data compare

Built from OpenBCM sdk-6.5.26-DNX.1 with `-D__DUNE_LINUX_BCM_CPU_PCIE__` and
`-D_SIMPLE_MEMORY_ALLOCATION_=0` (what the SDK's own `Make.pkg.88670` does for
this chip). 1722 files, 0 errors, 141,628,112 bytes, ELF 64-bit MSB MIPS64 rel2,
static. It now reports:

    BDE dev 0 (PCI), Dev 0x8375, Rev 0x11, Chip BCM88375_B0
    DMA pool size: 67108864 bytes.
    + 0: Device Reset and Access Enable      <- the CPS reset; PASSES now

**(PCI), not (I2C)**, and the `soc_dcmn_cmic_device_hard_reset` failure is gone.
The 134 `sal_i2c_init_fd` errors collapsed to one benign line. It reaches a live
`BCM.0>` prompt with `SchanOps=418`, `Timeout: Schan=0`, `Flags=0x203 attached
initialized`.

### The remaining blocker is a data compare, not a transport fault

`soc_jer_regs_blocks_access_check_dma` writes `0xaaff5500 + i` over 20 entries
into IRR_MCDBm via `soc_mem_write_range`, zeroes the buffer, reads it back, and
compares under `soc_mem_datamask_rw_get`. **jer_regs.c:220 is the COMPARE
branch**, so both range calls returned SOC_E_NONE: the DMA ran, reported DONE,
raised no error, and moved well-formed data.

    entry 0: received:7f5541      expected:7f5500

Only the low byte differs -- `7f 55` matches, so **byte order is not the fault**.
And `(0xaaff5500 + 0x41)` masked is exactly `0x7f5541`, the value belonging to
**index 65**, while only entries 0..19 were written. So this is an
**indexing/stride or field-mask** discrepancy against IRR_MCDBm.

To see that line, insert `debug soc init debug` into OUR copy of `jer.soc`
before the `INIT_DNX` at line 158. It cannot be done from the prompt: the check
runs only in the FIRST init, and a later `init soc` on an attached device skips
phase1 (only `soc_jer_regs_eci_access_check` reruns), so a second init only
*looks* like it passes.

### Eliminated on hardware, not by assumption

| hypothesis | disproof |
|---|---|
| TDMA waits on an interrupt | `tdma_intr_enable.BCM88650=0` changes nothing; `config show tdma` confirms the edit was read |
| DMA address translation wrong | `dma_alloc_coherent` returns `dma_handle 0x2400000 phys 0x2400000` -- the platform itself is identity-mapped |
| cached reserved mapping / coherency | a 4 MB uncached coherent pool fails identically |
| INTx never re-enabled after MSI fails | `ffn_bde_paxb_route_intx()` runs and restores CMICD_TO_PCIE_INTR_EN to 1 |
| `get_dma_info` layout mismatch vs OpenBCM | matches the reference BDE field for field; `d2 = 1 = USE_LINUX_BDE_MMAP` |
| `SYS_BE_OTHER` not reaching the SDK | for `device & 0xF000 == 0x8000` byte order comes from compile-time `_bus`; `socdiag.c:181` sets it and passes `&bus` |

### Two separate items

* `interrupt priority set: : Function not implemented` is a `perror()` from
  `sched_setscheduler(0, SCHED_RR, {90})` at
  `systems/bde/linux/user/linux-user-bde.c:1895` -- cosmetic; silence with
  `-DSAL_BDE_THREAD_PRIO_DEFAULT`. Its presence does prove the BDE interrupt
  thread was created, i.e. `polled_irq_mode=0`, which is NOT this chip's SDK
  default of 1. `polled_irq_mode=1` would remove MSI/INTx from the path.
* A SIGSEGV in `arad_mgmt_sw_ver_set_unsafe`, plausibly tied to the recurring
  `sw_state_max_size SOC property is not defined` warning.

### Reproducing a run (everything below is lost on a CP reboot)

Config/cmd files are in tmpfs and device nodes are devtmpfs. Stage binaries
through the NFS root, not scp -- the busybox CP has no sftp-server, and the CP
roots on `127.1.1.1:/opt/ffn-cproot-owrt` (rw), so writing
`/opt/ffn-cproot-owrt/usr/local/ffn/x` on the MP makes it appear at
`/usr/local/ffn/x`. The VM reaches the MP but **the MP cannot reach the VM**, so
stream with `ssh VM 'cat bin' | ssh MP 'cat > dest'` and verify by sha256.
`insmod ffn_bcm.ko` BEFORE `ffn_bde.ko dma_phys=0x30000000 dma_mb=64`; mknod both
BDE nodes; export `BCM_CONFIG_FILE` (CWD alone is not enough); run on a **pty**
because a static glibc block-buffers on a pipe.
