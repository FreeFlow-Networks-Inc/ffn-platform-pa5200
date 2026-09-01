# DP OCTEON memory: 32 GiB installed, 442 MB used

The dataplane Octeon in a PA-5220 (CN7885, 40 cores) has **32 GiB of DRAM**. Until
this change FFN booted Linux on it with **~442 MB**, i.e. 1.3% of the machine.

Measured, not assumed. The vendor's own DP console log
(`/opt/var.cp/log/pan/dataplane0-console-output.log` in the 5220 sysroot) records
u-boot sizing the DRAM at boot:

```
Base DRAM address used by u-boot: 0x80f000000, size: 0x1000000
DRAM: 32 GiB
```

and that same log shows the vendor's Linux taking only 1 GiB of it
(`Memory: 1019784k/1048920k available`), because the vendor passes `mem=1024`.

## Why FFN was getting 442 MB

FFN's DP boot line passed **no `mem=` at all**. On OCTEON that is not "use
everything" — it is "use the default", and the default is a C initialiser:

```c
/* arch/mips/cavium-octeon/setup.c */
static unsigned long long max_memory = 512ull << 20;
```

512 MB, minus the kernel image and its reservations, is the 442 MB that `free`
reported. This is the same cap that held the CP to 442 MB until it was given
`mem=8G`.

## Why `mem=32G` would be wrong

`mem=` on OCTEON is not an address ceiling. It is the size of the slice Linux
takes **out of `cvmx_bootmem`**:

```c
while ((boot_mem_map.nr_map < BOOT_MEM_MAP_MAX) && (total < max_memory)) {
        memory = cvmx_bootmem_phy_alloc(mem_alloc_size, ...);
        add_memory_region(memory, size, BOOT_MEM_RAM);
        total += mem_alloc_size;
}
```

Whatever Linux does not claim stays in the cvmx free list, and **that is where the
packet engine's memory comes from**. The FPA pools sized by `pktbuf=` and `wqe=`,
plus PKI, SSO and PKO3's own structures, are all allocated from it during
`cvmx_helper_initialize_packet_io_global()`. Claim all 32 GiB for Linux and the
dataplane dies at start-up with:

```
ERROR: cvmx_helper_mem_alloc failed size 93974400
ERROR: cvmx_fpa3_pool_populate: POOL 0:30 out of memory
ERROR: __cvmx_pko3_config_memory AURA create failed
cvmx_helper_initialize_packet_io_global() failed
```

With no FPA pools there is no packet engine, and the resulting symptom — a PCIC
doorbell that climbs while the instruction count stays at zero — looks exactly
like a misprogrammed ring and is not one.

**So the budget is: everything except a CVMX reserve.** `FFN_DP_MEM=30G` leaves
2 GiB. For scale, the vendor's own pool sizing (`pktbuf=86016,2048` +
`wqe=856600,128`) is about 286 MB, so 2 GiB is roughly seven times the largest
configuration anyone has run on this hardware. Lower `FFN_DP_MEM` if
`cvmx_helper_initialize_packet_io_global()` ever fails; raise it only with the
above error signature in mind.

## Why `ffn_reserve=` is mandatory here, not optional

FFN's MP-owned regions in DP DRAM are **bare physical addresses the MP picks**.
They are not `cvmx_bootmem` named blocks and never were. They were chosen when
Linux managed only 512 MB, so they sat outside it:

| region | base | size | when it is accessed | reserved? |
|---|---|---|---|---|
| `ffn-dpsh` control mailbox | `0x400000` | 64 KB (`RING_SIZE`) | CP writes, continuously, Linux up | yes |
| dpnet staging scratch | `0x500000` | 1 MB | CP writes at bring-up | yes |
| dpnet rings (`FFN_DPNET_DP_PHYS`) | `0x600000` | 2 MB | CP + DP, continuously, Linux up | yes |
| staged rootfs squashfs | `0x28000000` | ~19 MB | `ffn-dproot` **reads it from userspace**, Linux up | yes |
| staged kernel image | `0x21000000` | ~11 MB | `bootoctlinux` consumes it in u-boot, **before Linux** | **no** |
| bootloader mailbox | `0x6C000` | <256 B | u-boot only, **before Linux** | **no** |

The last two are the point of the live-access rule below: both are written by the
MP, neither is touched while the allocator is live, so neither is reserved.

Raise `mem=` and Linux manages those addresses. This was **measured on the CP**,
where the equivalent regions all read `KPF_BUDDY` in `/proc/kpageflags` — they
were sitting in the buddy allocator's free lists while `ffn_cpdpd` and
`ffn_pcnetd` were writing them through `/dev/mem`. Nothing catches it: this
kernel has `CONFIG_DEVMEM` without `CONFIG_STRICT_DEVMEM`, so the mmap succeeds
and the daemons scribble over whatever the allocator handed out.

The DP is the worse case of the two, because its **control mailbox is at 4 MiB**.
Any `mem=` above the 512 MB default puts the only way into the box inside managed
RAM.

The fix is the `ffn_reserve=<base>,<size>[;<base>,<size>...]` kernel parameter,
parsed in `plat_mem_setup()` before anything reaches `add_memory_region()`. The
bring-up scripts build it as:

```
ffn_reserve=0x400000,4M;0x28000000,${IMG_MIB}M
```

**Range 0 is a literal because its geometry is fixed.** 4 MiB is not padding
around the 64 KiB mailbox ring — it is the **BAR1 window granularity**.
`ffn_dpsh.py` documents it: "DP phys 0x400000 sits in BAR1 window index 1 (each
index covers 4 MB)". 4 MiB is exactly what the CP can address through that index,
so the window is the honest boundary. It also holds the dpnet scratch at
`0x500000` and the rings at `0x600000`, and ends exactly at the kernel's link
address `0x800000`.

**Range 1 is DERIVED from the payload, not written as a literal.** The rootfs
squashfs changes size every time it is rebuilt — 19 MiB as Buildroot against
56 MiB as the old CentOS tree. A literal that stops covering it is the original
bug back again, wearing a reservation line as camouflage: the boot line still
looks correct while the allocator quietly takes the uncovered tail. So `mib_up()`
sizes the blob from the blob itself, rounded up to a whole MiB, in the same script
that stages it.

Three properties of that sizing are deliberate:

* It is called in the **main shell**, not inline inside `${...:-}`. A command
  substitution that fails only exits its own subshell, so an inline call would
  yield `0x28000000,M` on a missing payload — which `memparse` rejects. (The
  kernel-side half of that hazard is fixed too: a malformed entry now costs only
  itself and is reported at `pr_err`, where it used to abandon every entry after
  it. Both halves were needed — the script must not emit a malformed token, and
  the parser must not amplify one.)
* A missing or empty payload is **fatal**, not defaulted. There is no safe number
  to guess for a blob that is not there.
* Staging goes through `stage <addr> <path> <reserved-MiB> <timeout>`, which
  **re-checks the size against what was actually reserved** and refuses to load a
  payload larger than its own reservation. Sizing and staging are two reads of
  the same file some way apart in the script; if they ever disagree — the file
  was replaced, or a later edit stages a different path than the one that was
  sized — the reservation ends up smaller than the blob, which is the original
  bug with a reservation line vouching for it. A smaller payload is fine and
  proceeds; only outgrowing the reservation stops the bring-up.

`ffn-dp-boot.sh` stages only the kernel, and the kernel staging area needs no
reservation, so its whole line is `ffn_reserve=0x400000,4M` — no derived sizes and
none of the sizing machinery. It also does not reserve `0x28000000`: nothing in
that script owns it, and reserving an unowned region is the pre-authorised slack
the rule below warns about.

**A DP kernel without the `ffn_reserve=` patch must not be booted with a raised
`mem=`.** Check before booting: `strings vmlinux | grep -c ffn_reserve` must be
non-zero.

### The test is LIVE ACCESS, not "the MP writes it"

A region needs reserving when **any** access to it — read or write — can happen
while the kernel's allocator is live. "Live writer" is too narrow: a consumer
that *reads* the region after mm is up is exposed exactly like a concurrent
writer, because the allocator can have handed those pages out before the reader
gets there.

That is what separates the two staged payloads on this box, despite both being
written by the CP before boot:

* `0x28000000` is read by `ffn-dproot` with `dd if=/dev/mem`, **from userspace,
  long after the allocator is live**. Load-bearing.
* `0x21000000` is consumed by `bootoctlinux` **in u-boot, before Linux exists**,
  and nothing reads it afterwards. Not reserved — see below.

### Considered and deliberately NOT reserved

**The kernel staging area, `0x21000000`.** `bootoctlinux` copies the ELF to the
link address at `0x800000` while still in u-boot; by the time Linux runs, the
image there is dead weight that nothing reads. It fails the live-access test, so
it is not reserved even though the allocator demonstrably *does* reach it once
`mem=` is raised (measured `0x400` at that address on the CP at `mem=8G`). Being
reachable is not the same as being at risk. Re-add it if any flow ever stages a
kernel while Linux is running — nothing does today, and every re-boot path starts
with `oct-remote-reset`.

**The bootloader mailbox at 0x6C000.**

`ffn_dpsend.py`, `ffn_dpmbox.py` and `ffn_dpmem.py` all use DP DRAM `0x6C000` —
`MBOX_STATE` at `0x6C000`, `MBOX_LEN` at `0x6C004`, `MBOX_CMD` at `0x6C008` with a
247-byte maximum, so under 256 bytes in total. It sits **below** range 0 and is
therefore not covered by anything above.

That is deliberate. It is the **bootloader's** mailbox, live only while u-boot is
polling it, and every path that writes it runs *after* `oct-remote-reset` — by
which point Linux on the DP is already gone. There is no writer racing the
allocator, which is what separates it from the `ffn-dpsh` mailbox at `0x400000`
and the dpnet rings at `0x600000`, both written continuously while Linux runs.
`ffn_dpmbox.py` may read it with Linux up, and reads are harmless.

Reserving it anyway would protect nothing and add slack, which the rule below
says not to do. Recorded here so the next person does not have to re-derive the
lifecycle, or add it on the reasonable-looking grounds that it is an address the
MP writes. The boot verification below reads its page flags regardless, so the
assumption is checked rather than trusted.

### What a reservation does not do

It keeps the **kernel's allocator** out of a range. It does not arbitrate between
two FFN users of the same bytes. If two of our own components ever claimed
overlapping addresses, reserving the range would remove the allocator — the one
party whose involvement makes the collision visible — and leave both writers
corrupting each other silently. So every entry in the table above must be a
region with exactly one owner, verified in the source, before it is added here.
Reserving a range to make a symptom go away converts a loud bug into a quiet one.

## MEASURED, on the live DP (2026-09-01)

**The headline: `MemTotal: 30904712 kB` — 29.5 GiB, against 442 MB before.** A 70x
increase, verified end to end: session up, Buildroot squashfs mounted from DRAM,
`chroot` into `GNU bash 5.2.37 (mips64-buildroot-linux-gnu)`, burst test clean.

### The `;` in the boot line does not survive u-boot — read this first

`;` is **u-boot's command separator**. An unescaped one truncates the boot line
*there*, and u-boot cheerfully runs the first half and boots, so nothing reports
an error. Measured, with a bare `;`, the kernel received:

```
bootoctlinux 21000000 ... rw ffn_reserve=0x400000,4M
```

Everything from the `;` onward was gone: the second reserve range **and
`pktbuf=8192,2048 wqe=256,128`** — the FPA pool sizing whose absence is precisely
what kills the packet engine. So the bug was never only about reservations; any
`;` anywhere in that line silently amputates the rest of it.

Escaping as `\\;` fixes it, and the kernel still receives a plain `;`:

```
bootoctlinux 21000000 ... rw ffn_reserve=0x400000,4M;0x28000000,19M pktbuf=8192,2048 wqe=256,128
```

The scripts now escape it themselves, with shell built-ins rather than `sed`
(the CP's vendor root has none). **Anything else that assembles an OCTEON boot
line has the same exposure** — this reaches the DP through the bootloader
mailbox, and the CP's through `ffn_octboot.py`, but both land in the same u-boot
parser.

### Two forms, and why this ships the escaped one

Newer kernels accept `ffn_reserve=` **repeated**, which needs no separator and so
cannot be damaged in transit:

```
ffn_reserve=0x400000,4M ffn_reserve=0x28000000,19M
```

That is the better form in isolation — a hazard that cannot arise beats one every
caller must remember to avoid. It is not what these scripts emit, because the two
forms fail differently under version skew and only one of them fails loudly:

| form | old parser | new parser |
|---|---|---|
| escaped `\\;` list | works | works |
| repeated params | **silently keeps only the first** | works |

The old parser located the parameter with a single `strstr()`, so a second
`ffn_reserve=` was ignored with no warning at all. A boot line using repetition
against a kernel that predates the outer-loop parser therefore loses every range
but the first, invisibly — the same silent-truncation failure this whole
mechanism exists to prevent, just relocated from u-boot into the kernel.

The escaped list works on both, and the escaping lives in exactly one place per
script with a test behind it. So: **emit the escaped form; accept either.** Switch
the default to repetition once no kernel predating the outer-loop parser is in
service anywhere — and until then, do not hand-write a boot line in the repeated
form without checking which parser the target kernel has.

### There is a FOURTH kpageflags signature

| value | meaning |
|---|---|
| `0x100000` | **`KPF_NOPAGE`** — no `struct page` at all; `pfn_valid()` is false, the PFN is outside any populated sparsemem section |
| `0x800` | `KPF_MMAP` artifact — the section is populated so a `struct page` exists, but it was never initialised, so `page_mapcount()` reads 1 |
| `0x0` | `struct page` initialised and **not in the allocator** — either in use, **or excluded from the memory map by `ffn_reserve=`** |
| `0x400` | `KPF_BUDDY` — in the buddy allocator's free lists |

**`0x0` is ambiguous, and that matters for judging a reservation.** A range
reserved inside an already-populated section gets its `struct page` initialised by
`memmap_init` and is then simply never handed to the allocator — so it reads `0x0`,
indistinguishable from an ordinary allocated page. Measured on the CP, where the
reserved ring bases read `0x0`, not `0x800`.

So **`/proc/iomem` is the authoritative check and kpageflags is corroboration.**
They fail independently, which is the only reason a working reservation is not
mistaken for a broken one: a single-check procedure keyed on `0x800` would have
read `0x0` on the CP, concluded the parameter did nothing, and sent someone
debugging a kernel that was correct.

`0x100000` was not in the three-signature calibration and is what both staged
regions actually read. It is a stronger statement than `0x800`: not merely "the
allocator never got it", but "the kernel has no page structure for this address".

### The sweep, identical on both boots

```
0x0006C000  bootloader-mbox    0x800      below the floor
0x003FF000  below-range0       0x800
0x00400000  dpsh-mailbox       0x800      range 0 — below the floor, as predicted
0x00600000  dpnet-rings        0x800
0x007FF000  top-of-range0      0x800
0x00800000  kernel-link        0x0        first managed page: kernel code
0x21000000  kernel-staging     0x100000   KPF_NOPAGE
0x28000000  rootfs-staging     0x100000   KPF_NOPAGE
0x8EF00000  first-managed      0x400      lowest page the allocator holds
0x90000000  control-page       0x0
```

### What this means: the A/B stop condition fired

Boot A did **not** show `0x400` at `0x28000000`, so the transition test could not
run. The reason is now measured rather than guessed: Linux takes its 30 GiB from
`8ef00000` upward, leaving `0x1360000`–`0x8eefffff` (~2.37 GiB) to cvmx — and both
staged regions sit in that gap, `0x28000000` with about **1.73 GiB of headroom**
below the lowest managed page. The kernel says so itself:

```
FFN: ffn_reserve 0x400000+0x400000: only 0x0 bytes were excluded
FFN: ffn_reserve 0x28000000+0x1300000: only 0x0 bytes were excluded
```

Both ranges parsed correctly and neither intersected anything the allocator was
claiming. Per the stop condition above, the response is to **stop claiming
verification, not to stop shipping the reservation**: at `mem=30G` these regions
are not at risk, the reservation is insurance against a future `mem=` that eats
into that 1.73 GiB, and the reason for keeping range 0 never depended on the
measurement anyway.

Honest summary of what is proven and what is not:

* **Proven:** 30 GiB boots, the full DP stack works on it, `ffn_reserve=` parses
  and reports accurately, and the `;` truncation is real and now fixed.
* **Not proven:** that `ffn_reserve=` protects anything on this hardware at this
  `mem=`. Nothing needed protecting. To exercise it, raise `mem=` past roughly
  31.7 GiB so the allocator descends to `0x28000000` — worth doing once,
  deliberately, if the reservation ever needs to be trusted rather than merely
  present.

## Verifying after the boot

### This needs TWO boots, and the first one is what makes the second mean anything

The obvious check — "after the fix, each reserved base reads `0x800`" — is
**unfalsifiable on its own**. `0x800` is also what an address that was never
managed reads. A base that was outside managed RAM all along, with
`ffn_reserve=` silently ignored, produces exactly the same output as a base the
parameter successfully protected.

That is not hypothetical here. At the old 442 MB boot the DP's System RAM is
`00800000-0135efff`, `dff00000-efffffff` and `f0001000-ffefffff`, so `0x21000000`
and `0x28000000` — the two load-bearing reservations — are **already** unmanaged
and already read `0x800`. Measuring them only after the fix would prove nothing.

So the check is a **transition**, not a state:

| boot | boot line | `0x28000000` (reserved) | `0x21000000` (positive control) |
|---|---|---|---|
| **A** | `mem=30G`, **no** `ffn_reserve=` | `0x400`, inside `System RAM` | **not** `0x800` |
| **B** | `mem=30G` **with** `ffn_reserve=` | `0x800`, **hole** in `/proc/iomem` | **not** `0x800` — unchanged |

`0x21000000` staying managed across both boots is the expected result, not a
failure: it is deliberately unreserved, and its job is to show the allocator
reaches that region at all. Without it, "the reserved base went `0x400` → `0x800`"
cannot distinguish the reservation working from the memory map having moved for
some unrelated reason.

**Assert "not `0x800`", never `== 0x400`.** A managed page reads `0x400` when free
and `0x0` when the allocator has handed it out, and both are correct for a
control — only `0x800` means out of the allocator's reach. Pinning the exact value
invites a spurious red on a boot that merely allocated more before the sweep ran,
and a control that cries wolf gets deleted by the next person. Note this groups
the three signatures differently from the reserved rows above, where `0x0` and
`0x400` genuinely differ: for a region with live access, allocated is worse than
free; for a control, the distinction is irrelevant and collapsing it is the
point.

Boot A is the one that proves the hazard is real on this hardware rather than
inherited from the CP's measurement; boot B proves the parameter fixed it.

If A does **not** show `0x400` at `0x28000000`, there are three readings, and
only the first two are problems: `mem=30G` did not take; the allocator does not
reach these addresses; or the region falls inside the 2 GiB left to cvmx, in
which case it is not at risk today but is still ours to protect. The response to
all three is to **stop claiming verification**, not to stop shipping the
reservation — the reason for keeping it never depended on the measurement.

Boot A is safe to run as long as nothing **writes** those regions while it is up:
do not mount the big rootfs on boot A. Reading `/dev/mem` there is harmless; the
staged kernel at `0x21000000` has already been consumed by `bootoctlinux` by then.

Range 0 is expected to read `0x800` on **both** boots, because it sits below the
DP's managed floor. That is not a failed reservation — see the floor discussion
below. It is the reason range 0's justification is "live writer" and not "at
risk".

### The checks

1. `free` / `/proc/meminfo` — MemTotal should be near 30 GiB, not 442 MB.
2. `/proc/iomem` — there must be a **hole** at each reserved base; if `0x400000`
   falls inside a `System RAM` range the reservation did not take.
3. Per region, the direct test:
   ```sh
   for b in 0x6C000 0x3FF000 0x400000 0x500000 0x600000 0x7FF000 \
            0x800000 0x21000000 0x28000000 0x40000000; do
       printf "%-12s " $b
       dd if=/proc/kpageflags bs=8 skip=$(( $b / 4096 )) count=1 2>/dev/null | od -An -tx8
   done
   ```

   **There are three signatures, not two** — "not `0x400`" is not one answer:

   | value | meaning |
   |---|---|
   | `0x100000` | no `struct page` at all — outside any populated sparsemem section |
   | `0x800` | populated section, `struct page` never initialised (below the floor) |
   | `0x0`   | initialised and not in the allocator — in use **or reserved** |
   | `0x400` | **free in the buddy allocator** — the dangerous state |

   `0x800` is the confusing one: bit 11 is `KPF_MMAP`, which reads as "something
   has this mapped" and sounds alarming when it means the opposite. An
   uninitialised `struct page` has `_mapcount == 0`, so `page_mapcount()` returns
   1 and `stable_page_flags()` sets the bit as an artifact. Measured on the CP:
   uniform `0x800` for every PFN below its floor and never once at or above it.

   The one thing a reserved base must **never** read is `0x400`. Which of the
   other three it reads depends on where it sits, and **none of them proves the
   reservation worked** — for that, look for a hole in `/proc/iomem` at the
   requested base and of the requested size. `0x40000000` is the control and
   should read `0x400`.

   **The DP's floor is not the CP's.** The CP's lowest System RAM range is
   `00400000-007fffff`, so on the CP `0x400000` is the *first managed page*. The
   DP's lowest range is `00800000-0135efff` — its whole range 0 sits below the
   floor. `0x3FF000` vs `0x400000` vs `0x7FF000` vs `0x800000` in the sweep above
   is what confirms that still holds at `mem=30G`; do not assume the CP's answer
   transfers.

   Range 0 is expected to read `0x800` on the DP because it sits below the floor.
   It is kept because it has **live access** — the CP writes the mailbox and the
   rings continuously while Linux runs — not because the allocator reaches it.
   Whether a region is at risk today and whether it is ours to protect are
   different questions; the rule keys on the second.

   `0x21000000` is in the sweep as a **probe, not a reservation**. It should read
   `0x400` at `mem=30G`, which demonstrates the allocator reaches that far — the
   fact that makes boot A meaningful for `0x28000000`. It is not itself reserved.

Then confirm the transports still work — `ffn-dpsh -c uname -a` and a ping across
`ffndp0` — because those are what the reservation exists to protect.
