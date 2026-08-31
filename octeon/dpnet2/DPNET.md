# ffn_dpnet -- CP <-> DP virtual Ethernet over PCIe

Gives the DP (CN7885, 40 cores) a network interface. Chained with the existing
MP <-> CP pcnet link, the DP is reachable from the MP entirely over PCIe:

    MP 127.1.1.1  --pcnet-->  CP 127.1.1.2 / 127.1.2.1  --dpnet-->  DP 127.1.2.2
       (Xeon-D)                     (CN7340, forwards)                (CN7885)

Everything is inside 127/8, which is non-routable. There is no physical-topology
path to any of it -- the same isolation boundary the MP's NFS export relies on.
The CP holds no IP on eth0/eth1 (see "The eth0 trap" below).

## Status

Working and measured on the live PA-5220:

| | |
|---|---|
| CP <-> DP ping | 5/5, 0% loss, 0.73 / 8.7 / 11.4 ms (min/avg/max) |
| MP <-> DP ping | 5/5, 0% loss, ~15 / 18 / 20 ms, ttl 63 (one hop via the CP) |
| CP -> DP TCP | 32 MB in 2.096 s = **16.0 MB/s (128 Mbit/s)** |
| integrity | 23,194 frames / 35 MB: **0 CRC failures, 0 drops, 0 tap drops** |
| flow control | 53 stalls, 0 frames lost (see "Backpressure, not loss") |

Latency is dominated by the poll interval on each hop, not the PCIe link. This
is a management link -- ssh, NFS, packages, telemetry. Forwarding-plane traffic
does not go this way; it goes through the FE100 / BCM88375 path.

## Files

| file | runs on | what |
|---|---|---|
| `ffn_dpnet_ring.h` | -- | region layout, geometry, single-writer rules |
| `ffn_dpnetd.c` | CP and DP | the daemon, both roles in one binary |
| `ffn_dpstage.py` | CP | pushes a file into DP DRAM over PCIe (bootstrap) |
| `ffn-dpnet-up.sh` | MP | brings the whole thing up, idempotent |
| `Makefile` | build server | static big-endian MIPS64, with a sanity check |

Build on the build server, which has
`mips64-linux-gnuabi64-gcc`. `make` also runs `make check`, which refuses a
binary that is not big-endian, static and 64-bit -- each of those has a plausible
failure mode and a silently little-endian build would look like flaky hardware.

## Region map

The rings live in **DP DRAM**, and the CP reaches them through the DP's PCIe
BAR1 index-1 window. That window was already programmed and already proven by
the ffn-dpsh shell channel, so dpnet adds no new BAR1 index and no new CSR
write. Index 1 maps DP phys `0x400000-0x7FFFFF` at the same offset in the BAR,
so for the CP a DP physical address in that range *is* its BAR offset.

    0x400000   64 KB   ffn-dpsh mailbox      (in use by the shell channel)
    0x410000  ~3.9 MB  unused tail of that MB
    0x500000    1 MB   staging scratch       (ffn_dpstage.py)
    0x600000    2 MB   dpnet region          (header + two rings)

All four boundaries are on a MB so the DP's `dd if=/dev/mem bs=1M skip=N`
read-out recipe works. The DP has **no System RAM below 0x800000** (confirmed in
its `/proc/iomem`: RAM starts at `0x00800000`), so none of this is memory the
kernel believes it owns.

Inside the region:

    +0x000000   header, 64 B
    +0x001000   C2D ring   CP produces, DP consumes
    +0x101000   D2C ring   DP produces, CP consumes

256 slots x 2048 B per ring.

## Protocol

Monotonic `head`/`tail` counters, index = counter % nslots. `head` is written
only by the producer, `tail` only by the consumer, and **the consumer never
writes into a slot at all** -- head/tail alone say what is ready, so there is no
ready flag to clear and no shared field. That is what makes the rings lock-free
with no atomics.

Each slot is `{u32 len, u32 crc, payload}`. `len` and `crc` deliberately share
one 8-byte group so the producer publishes both in a single 64-bit store: a
consumer can never pair this frame's length with the previous frame's CRC.
Producer order is payload, then `{len,crc}`, then a write barrier, then the
`head` advance -- so a consumer that reads `head` can never see a half-written
slot. Every frame is CRC32'd (IEEE/zlib), so a coherency slip is detected rather
than handed to the kernel.

Neither side caches `head`/`tail` locally. That costs one extra access per
operation and buys a real property: when the CP restarts and zeroes the
counters, the DP picks it up on its very next look, with no reset handshake to
get wrong. A difference beyond the ring size is treated as "the peer restarted
mid-flight" and resynchronised rather than used as an index.

### Endianness -- the one genuinely subtle part

Both CPUs are big-endian, so the shared layout is plain big-endian and **the DP
touches it natively with no conversion at all**. The CP, however, reaches it
through a PCIe path that **reverses the bytes within every aligned 64-bit
word**. So the CP -- and only the CP -- byte-swaps each 8-byte group on the way
in and out. This is not a guess: it is what ffn-dpsh has done reliably for its
mailbox, and `ffn_dpstage.py` staging a 798 KB binary that the DP then verified
by sha256 proves it end to end.

Consequence for the layout: a 64-bit group is the atom of this window, so
**every control field gets its own 8-byte group**. Combined with one-writer-per-
group, a read-modify-write of a group is always safe -- nobody else writes it.

### Backpressure, not loss

The first working version dropped 59 frames out of 34,798 when the ring filled.
Reading a frame out of the TAP and then dropping it is real loss that TCP has to
retransmit end to end; leaving it in the kernel's transmit queue costs nothing.
So the daemon now checks for ring space *before* reading from the TAP. While the
ring is full it also stops watching the TAP fd -- that fd is readable and will
stay readable, so polling it would spin; what unblocks the daemon is the peer
draining the ring, which is a clock event.

Measured effect on the same 32 MB transfer: **59 drops became 53 stalls with zero
frames lost, and throughput rose from 11.6 to 16.0 MB/s** -- faster *because* it
stopped dropping, since every drop cost a TCP retransmit and a congestion-window
cut. Flow control was the cheaper option in both directions at once.

## Bring-up

    /opt/ffn-ngfw-v2/octeon/dpnet2/ffn-dpnet-up.sh     # on the MP

Idempotent. It checks pcnet is up, stages the binary into DP DRAM, has the DP
`dd` it back out and verify the sha256, starts the CP end (which owns region
init) then the DP end, adds the transit routes, and pings from both the CP and
the MP.

Status at any time, from the CP:

    ffn_dpnetd --role cp --status

`kill -USR1` on either daemon logs traffic counters.

### The bootstrap problem, and how staging solves it

The DP needs the daemon's binary before it has any network to fetch it over.
`ffn_dpstage.py` runs on the CP, writes the file straight into DP DRAM through
the same BAR1 window (applying the 8-byte reversal), reads it back and compares
sha256; the DP then `dd`s it out of `/dev/mem`. 798 KB takes about 6.5 s, almost
all of it Python doing the byte reversal. Same mechanism ffn-dproot already uses
to pick up its squashfs.

## Gotchas found the hard way

**The eth0 trap.** The CP's physical `eth0` was carrying `127.1.2.1/24` from
earlier work. It has a lower ifindex than `ffndp0`, so it won the route and the
CP ARP'd for the DP out a dead physical port -- rings moving perfectly, ping
100% lost. `ip route get` was what showed it (`dev eth0` instead of `dev
ffndp0`). This also contradicted the documented invariant that the CP holds no
IP on eth0/eth1, so removing it was the right fix twice over. The bring-up
script now removes it every run.

**Source address on the transit route.** The MP's route to `127.1.2.0/24` picked
`the MP` as its source, so the DP replied to an address it has no route
to. Needs an explicit `src 127.1.1.1`.

**`route_localnet`** must be 1 on every interface carrying a 127/8 address, on
both ends and on the CP's pcnet side too, or the kernel treats the traffic as
martian.

**`ffn_cpsh -c` takes ONE LINE.** It appends an end-of-command marker and waits
for it; embedded newlines break that and the call hangs until timeout. Cost a
debugging cycle. Same for `ffn-dpsh -c`.

**Command substitution expands on the WRONG HOST -- count the shells.** A command
for the CP passes through two shells (MP bash, CP shell); a command for the DP
passes through **three** (MP bash, CP shell, DP shell). Every level of double
quoting is another chance for `$(...)` to be evaluated too early:

* unescaped, `kill $(pidof ffn_dpnetd)` runs `pidof` **on the MP**, finds
  nothing, and sends the target a bare `kill`;
* escaped once as `\$(...)`, it runs on the **CP** -- correct for `cp_run`, but for
  `dp_run` it hands the DP the CP's pid list, so the DP kills unrelated processes
  and its own daemon keeps `/dev/net/tun`.

Both failures look identical from outside: the replacement daemon dies with
`TUNSETIFF: Device or resource busy` while the link keeps working perfectly --
on the OLD binary. The fix is not more backslashes but **single-quoting the inner
command** so the middle shell cannot touch it:

    dp_run() { timeout 200 $CPSH -t 170 -c "ffn-dpsh -t 120 -c '$1'"; }

(so `$1` must not itself contain a single quote). And because this class of bug
hides rather than announcing itself, the bring-up script now *verifies* each end
logged "side running" instead of assuming the start worked -- which is what
finally made it visible.

**`pidof` on the CP counts zombies.** The CP's PID 1 does not reap orphaned
children, so a daemon that has exited stays in `/proc` forever and `pidof` keeps
reporting it -- `pidof` cannot be used as a liveness test here. Wait for
`ip link show ffndp0` to fail instead: the interface disappearing is both the
thing that actually matters and immune to the zombies.

**The CP runs Python 2.7.5**, not python3, and `python3` is not on its PATH.
`ffn_dpstage.py` is written for either.

**`net/if.h`, not `linux/if.h`.** They define `struct ifreq` incompatibly;
including both fails to compile. `linux/if_tun.h` on top of `net/if.h` is the
combination that works.

**`#if` cannot parse a cast.** `FFN_DPNET_RING_BYTES` is kept cast-free so the
compile-time "does the ring fit its slice" check can evaluate it.

## Hardware reference material

The vendor-side facts this transport was checked against -- the recovered
`pci_dma` protocol, its register offsets and doorbell values, and the vendor's
own CP/DP addressing plan -- are **not** in this repository. They are hardware
reference material, and they live in the `hw/pa5220` submodule:

    docs/dpnet-pa5220-reference.md

That separation is deliberate. This repository is FFN's own code; anything
derived from analysing the commercial product it replaces is reference material
about specific hardware, and is versioned separately.

