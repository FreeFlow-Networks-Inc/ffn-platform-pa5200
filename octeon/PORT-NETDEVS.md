# DP trunk driver: shared SSO owner and per-port kernel netdevs (phase 1)

**Status 2026-10-21: built, not deployed.** The module in `octeon/kctl`
(`ffn_dp_packet_init.c` + `ffn_dp_dma.h` + `ffn_dp_port.h` + `ffn_dp_trunk.h`)
builds clean with `-Werror` against the DP tree `6.18.49-ffn-dp-e4bfcbb72e20`.
With its default parameters it behaves exactly as the module the PA-5220 runs
today (md5 `c7bf7034`, `rx_groups=8`): the first deployment is a parameter
flip, not a rebuild. Nothing here has run on silicon yet; the window steps and
the rollback are at the end.

Read with `PACKET-PATH.md` (how PKI/SSO/PKO came to be owned by this module),
`octeon/native/README.md` (what ffn-portd does with `ffnpkt0` today),
`DP-FORWARDER.md` and `dpfwd/ffn_dp_io_octeon3.c` (the userspace dataplane
that is the other half of the sharing contract) and `FACEPLATE-MAP.md` (the
system ports).

## Why

The owner's targets (2026-10-21): 40G Internet in future, about 17 Gbit/s
with inline inspection, near line rate with inspection off. The DP is a
CN7885 at 1600 MHz with 40 cores: 64 G cycles per second for the whole chip.
At 17 Gbit/s of 800-byte packets (2.7 Mpps) that is 24k cycles per packet
across all cores; at 1500 bytes, 45k.

The path every transit packet takes today costs about **64k cycles**
(measured: three cores busy at about 75 kpps, 900 Mbit/s, 2026-10-21), and it
costs them on three threads with four user/kernel crossings in between. That
is why the TAP hop cannot be the inspected path and why the kernel's own
path (control, exception, management, VPN, and whatever is not offloaded)
needs to reach the ~10 Gbit/s class on its own. The design decided for it:

* the FE100 carries offloaded sessions (the only path to 40G full duplex);
* an own run-to-completion userspace dataplane on the DP cores will carry
  inspected traffic, taking packets straight from the SSO on its own groups;
* the kernel keeps control, exception, management and VPN traffic on
  per-port netdevs served by this driver, with no TAP in between;
* this driver is the one owner of PKI, SSO and PKO, serving both.

Phase 1 (this document) builds the driver side of that: group ownership with
a runtime switch, the punt contract, per-port netdevs beside `ffnpkt0`, the
kernel's descriptor queues, and the control interface. Phase 2 moves
ffn-portd and the core network owner onto it.

## 1. Group ownership

### The kernel's groups

The module takes `rx_groups` SSO groups (a power of two, default 8): groups
`0..rx_groups-1`. One kernel thread per group, `ffn-packet-rx<g>`, pinned to
one CPU (CPUs 1..8 on the box today), does `GET_WORK` with `NOWAIT` on that
group and nothing else. **A kernel thread never asks for work on any other
group.** The classifier adds `ilog2(rx_groups)` low bits of the flow tag to
the group number, so one flow stays on one thread, in order, while flows
spread.

SSO resources are provisioned once for all 256 groups at `prepare-sso`
(`sso_resources()`: IAQ and TAQ reserve thresholds per group, the XAQ chain
heads from pool 62 / aura 1022, 570 buffers). `SSO_AW_CFG[RWEN]` is set when
`ffnpkt0` opens. An application's groups therefore already exist and have
their reserves; what they lack is deep backlog (570 XAQ buffers serve all
256 groups), which phase 2 sizes when the application exists.

### Steering: `kernel` or `app`

Data traffic is everything the parser sorts into style 8 (untagged) or 9
(VLAN-tagged). Both styles point at QPG entry 8, and QPG entry 8 is the one
thing the steering switch rewrites:

| `steer`            | QPG 8 `GRP_OK/GRP_BAD` | `GRPTAG_OK/BAD` (tag bits added) | groups hit          |
|--------------------|------------------------|-----------------------------------|---------------------|
| `kernel` (default) | 0                      | `ilog2(rx_groups)` = 3            | 0..7, the kernel's  |
| `app <base> <n>`   | `base`                 | `n`                               | base..base+2^n-1    |

`steer app` is accepted only when the set is outside the kernel's groups,
aligned to its own size and inside 0..255 (`ffn_app_groups_valid`). It is a
live QPG write; the SDK's own vsys code rewrites QPG entries on a forwarding
box the same way. `steer kernel` puts it back. Both are one write to
`/sys/kernel/debug/ffn_dp_packet_init/control`.

FE100 return messages (styles 10 and 11, the DMACL parser-CAM match) use QPG
entry 9, which always names the kernel's groups. So `ffnret0` and the trunk's
return decoder keep working whoever owns the data path. Nothing else
distinguishes a return from data before the SSO; the PCAM does.

What the application must then do: every frame of styles 8 and 9 reaches its
groups, including LACP and LLDP from LAG members, ARP, the management
sessions' TCP, the VPN's UDP. **Whatever the kernel needs, the application
punts.** The kernel does not see it otherwise.

### The punt contract (application to kernel)

A punt is an SSO `ADD_WORK` to a kernel group with a work-queue entry the
kernel cannot tell from the PKI's. The kernel receive path reads exactly
this, in this order (`trunk_receive` and `ffn_wqe_decode` in `ffn_dp_port.h`):

| WQE word | bits   | field          | what the kernel requires                                                |
|----------|--------|----------------|-------------------------------------------------------------------------|
| 0        | 59:48  | `AURA`         | **1021**, the kernel packet aura (pool 61); anything else is `bad_dma` + fault |
| 0        | 31:24  | `BUFS`         | 1..64, the number of buffers in the chain                               |
| 0        | 5:0    | `PKIND`        | **8**, the trunk's port kind                                            |
| 1        | 63:48  | `LEN`          | packet bytes including the 4-byte envelope, 14..3584 or the entry is dropped |
| 1        | 43:34  | `GRP`          | the kernel group the entry was added to (a mismatch is counted, not dropped) |
| 1        | 33:32  | `TT`           | **2 (UNTAGGED)**; an ordered/atomic tag is counted in `punt_tagged` and released by the kernel's next `GET_WORK`, costing only that flow's ordering |
| 1        | 31:0   | `TAG`          | ignored                                                                 |
| 2        | 10:0   | `ERRLEV`, `OPCODE` | 0, or the entry is dropped (counted in `rx_drop_per_group`) with its buffers freed |
| 3        | 63:48, 41:0 | first buffer `SIZE`, `ADDR` | the packet pointer; `ADDR` at least 8 bytes into a pool-61 buffer |
| 4        |        |                | not read                                                                |

Buffers: every buffer of the chain must be a **pool 61** buffer (4096 bytes,
aura 1021); the kernel checks exact membership of each pointer against its
own table (`trunk_buffer`), including the WQE's own buffer, and refuses
anything else by faulting the trunk (`bad_dma`, `error -EFAULT`, PKI
disabled): a foreign pointer is a programming error, never freed
speculatively. A chained buffer's next pointer is the 8 bytes immediately
below its data (`ADDR - 8`), the PKI's own link layout. The WQE normally
lives in the first buffer (`first_skip` 5 words), but it may be a separate
pool-61 buffer: the kernel frees it once either way.

Ownership: the kernel **copies** the packet into an skb (see §3, copies) and
then frees every buffer of the chain to aura 1021, including the WQE's,
whatever the outcome (delivered, dropped as errored, oversized, down
device). After `ADD_WORK` the application owns nothing of the entry.

Ingress port: the packet data begins with the receive envelope exactly as
the PKI delivered it, `[dest16 = 24][src16 = ingress system port]`, then the
Ethernet frame. The kernel demultiplexes by `src16`; it never consults the
WQE for the port. A punt that originates in the application (not a received
frame) names its port the same way, with a 4-byte envelope it writes itself.

How to submit: the application links the SDK executive (`dpfwd` does), and
`cvmx_pow_work_submit(wqe, tag, CVMX_POW_TAG_TYPE_UNTAGGED, qos 0, grp)` on
its CN78XX branch is the `ADD_WORK` with the WQE's `GRP` and `TT` set. The
kernel issues no `ADD_WORK` itself; nothing in this module depends on the
store encoding, only on the entry and buffer contents above.

Allocation for application-originated punts: `cvmx_fpa3_alloc` from aura
1021, which is the aura every data packet already came from under `steer
app` (QPG 8 keeps `LAURA` 1021 in both modes). Pool 61 is 512 buffers; the
application's own traffic must not drain it (phase 2 gives data traffic its
own, larger aura; the punt contract stays aura 1021).

### The kernel's PKO queues

`tx_queues` (default 1, up to 8) descriptor queues, **DQ 0..tx_queues-1**,
under one scheduling chain: L1 SQ0 (link 12 = BGX2/LMAC0, channel 0xa00) →
L2 SQ0 → L3 SQ0 → L4 SQ0 → L5 SQ0 → the DQs. One DQ is a single child at
strict priority 0 (as today); several are siblings at priority 0 under L5
SQ0 with `RR_PRIO` 0, the SDK's "Fair-RR" (`cvmx_pko3_sq_config_children`
with `stat_prio_count -1`). Every netdev of the driver has `tx_queues`
transmit queues and queue *i* is DQ *i*; `ndo_select_queue` returns
`cpu % tx_queues`, so a sending CPU always hits the same DQ and the DQ lock
is contended only by CPUs that share a queue. The LMTDMA data word names the
DQ (`ffn_pko_lmtdma_word`: DQ at bits 25:16, per the SDK's
`cvmx_pko_lmtdma_data`), and each CPU's CVMSEG scratch is its own.

The application owns **DQ `tx_queues`..1023** and every scheduling queue
beyond index 0 at levels 2..5. It attaches its own L2 SQ under L1 SQ0: L1
SQ0 keeps `PRIO_ANCHOR 0, RR_PRIO 15` with the kernel's L2 SQ0 at strict
priority 0, so the application's L2 queue takes priority 1..8 (strict,
below the kernel) or 15 (round robin with nothing). That order is
deliberate: the kernel's traffic is control and exception traffic, low in
volume, and it must never queue behind 40 Gbit/s of transit. The kernel
never touches L1 SQ0 after `prepare-pko-queues`; the PKO MAC 12 FIFO (10 KiB)
and the channel LUT are shared facts, set once.

Completion: the kernel's descriptors end with `SEND_MEM` writing zero to the
last 8 bytes of the envelope buffer; the first receive thread reaps them
(`trunk_reap`) and frees the pool buffer, and the skb when the frame was
gathered. `DF=1` on every `SEND_HDR`: PKO frees nothing of the kernel's.

### The reverse direction: the kernel's own transmit under `steer app`

Nothing changes. A port netdev builds the ITMH for its destination system
port and hands the descriptor to one of the kernel's DQs, whoever owns the
receive side. The application never sees a kernel transmit, has no hook in
it and cannot block it; the only shared resource is the MAC's FIFO and the
L1 scheduler, where the kernel has priority. The switch, not the
application, decides where the frame goes (the ITMH names a system port the
switch's TM resolves). If the switch sends a copy back to the DP (a LAG
member's reflection, a return), it arrives through the PKI like anything
else.

## 2. Per-port netdevs

Module parameter `port_netdevs=1` (default 0). With 0 the driver registers
`ffnpkt0` only and delivers everything to it, as today. With 1 it also
registers, at `prepare-trunk`:

| netdev          | kind    | system port (receive source, transmit destination) |
|-----------------|---------|----------------------------------------------------|
| `ffnp1`..`ffnp24` | port  | `FACEPLATE-MAP.md`: p1 28, p2 13, p3 14, p4 15, p5 16, p6 1, p7 18, p8 19, p9 6, p10 21, p11 22, p12 23, p13 7, p14 11, p15 36, p16 27, p17 10, p18 29, p19 30, p20 31, p21 32, p22 33, p23 34, p24 35; `port <n> sysport <s>` overrides (the copper profile does for 1..4 today) |
| `ffnae<id>`     | LAG     | `0x8000 | tid` (`lag <id> add <tid>`), created on demand, never freed |
| `ffnret0`       | return  | source 20 (`return <s>` overrides) or the ITMH/CMH signature `xx 00 10 00` |
| `ffnpkt0`       | trunk   | everything, raw, as today                           |

All are registered **down with carrier off** in the init namespace; the
control plane moves and renames them (`ip link set ffnp5 netns ffn-data
name p5` is legal, nothing is `netns_immutable`).

### Receive

Each group's thread validates the chain, copies the frame into an skb and
calls `trunk_deliver_skb`: `skb_record_rx_queue(skb, g)` first, so AF_PACKET
fanout and RPS keep a flow on one consumer, then the envelope decides:

* dest ≠ 24 or no parse: `ffnpkt0` only (today's path);
* source = the return port, or the return signature: `ffnret0`, the frame
  **with** its 4-byte envelope, `ETH_P_802_2`, `PACKET_OTHERHOST`, so
  ffn-portd's decoder socket reads what it reads from `ffnpkt0` today;
* source in the map: the port's netdev, envelope stripped, `skb->dev` set
  and `eth_type_trans` run on the receive thread, `netif_rx`; a port that is
  a LAG member delivers to `ffnae<id>` instead, unless the frame is LACP or
  LLDP (ethertype 0x8809/0x88cc), which stays on the member so ffn-portd's
  control socket keeps seeing it; a member not in the LAG's `collect` mask
  drops data (counted on the LAG);
* source unknown (count `unmapped`): `ffnpkt0`.

What `ffnpkt0` still gets is the `deliver` mode: `trunk` (everything, ports
get nothing), `both` (the default when `port_netdevs=1`: a clone to the
port, the original to `ffnpkt0`, which is how the first window observes the
demultiplexing without touching the live TAP path) and `ports` (phase 2:
only what no port takes). A port netdev that is down counts `rx_down` and
drops, so the `ports` file proves demultiplexing before anything is up.

### Transmit

`port_xmit`: destination = the port's system port; for a LAG in `hardware`
mode `0x8000|tid` with the 8-bit load-balance key folded from
`skb_get_hash` into RAW_DSA bytes 1..3, exactly where the aggregate owner
puts it (the switch's field entry copies it into the LAG key); for a LAG in
software mode, the member picked by `hash mod distributing members`, as the
owner's rendezvous selection does today. `trunk_send` builds the 24-byte
envelope (`ffn_tx_envelope`: ITMH `01 dest16 00`, both MACs, 8 RAW_DSA
bytes), pads the Ethernet frame to 60 bytes before the 12 envelope bytes
(a frame the switch strips to under 60 would be a runt), takes the DQ of
the queue the stack chose, and issues `SEND_HDR` + gather + `SEND_MEM`.
LACP PDUs leave by the member netdevs because ffn-portd sends them there;
the LAG netdev never sends to a member it is not distributing on.

`ffnpkt0` transmit is unchanged: the user supplies the whole envelope and
the driver admits only `01 .. .. 00`, DSA byte 0 and bytes 4..7 zero
(`ffn_tx_envelope_valid`). `ffnret0` drops every transmit.

### Carrier, MAC, statistics, control

Carrier and MAC come from the control file (the CP owns the switch link
state; ffn-portd already learns it through `links`): `port <n> carrier
up|down`, `port <n> mac xx:..`, the same for `lag <id>`; `ip link set
address` works too (`eth_mac_addr`). `ndo_get_stats64` sums per-group
receive and per-queue transmit counters of the netdev; the `ports` file
(`/sys/kernel/debug/ffn_dp_packet_init/ports`, JSON) lists every netdev
with `rx/rx_bytes/rx_drop/rx_down/tx/tx_bytes/tx_drop`, LAG membership and
gates, the DQs (`tx/done/drop/open`) and the control state. The `status`
file gains `tx_queues`, `port_netdevs`, `deliver`, `steer`, `app_groups`,
`unmapped`, `punt_tagged` and stays under the 4096 bytes its readers allow.

The control file, one command per write, root with `CAP_SYS_RAWIO`, rtnl
then the initializer's lock:

```
steer kernel | steer app <base> <log2n>
deliver trunk | ports | both
return <sysport>
port <n> sysport <s> | carrier up|down | mac xx:xx:xx:xx:xx:xx
lag <id> add <tid> | del | member <n> add|del | collect <n> on|off
lag <id> distribute <n> on|off | hardware on|off | carrier up|down | mac xx:..
```

Ranges and the parse are `ffn_ctl_parse` in `ffn_dp_port.h`, tested on the
host. A rejected command returns `EINVAL`, a port command without
`port_netdevs` `ENODEV`, a system port already mapped `EEXIST`, a member of
another LAG `EBUSY`.

### Zero copy: where a copy remains and why

* **Receive copies once**, from the pool buffer into a fresh skb
  (`skb_put_data`), as today. The pool is 512 coherent 4096-byte buffers
  allocated at `prepare-dma`, published to the FPA with exact-membership
  accounting, and every buffer must come back to aura 1021 or the pool
  shrinks for good. An skb handed to the stack lives as long as a socket or
  a qdisc likes; building it around the pool buffer (`build_skb`) would
  either free our memory to the page allocator when the skb dies or need a
  destructor that refills the aura, i.e. a refillable page-backed aura with
  its own accounting. That is a change to the DMA layer with its own test
  on the box, so it is phase 2, and the doc says so rather than pretending.
  The copy is also what makes the exact-membership check cheap to keep.
* **Transmit copies once by default** (`tx_gather=0`): envelope and frame
  into one pool buffer, the path proven by 31 M frames on the box.
  `tx_gather=1` writes only the 24-byte envelope (and the pad) into the pool
  buffer and gathers the payload from the skb where it is: three gather
  segments, the skb held until `SEND_MEM` completes, then
  `dev_consume_skb_any` from the reaper. The DP kernel is DMA-coherent
  (`CONFIG_DMA_NONCOHERENT` unset; OCTEON's L1 is write-through and the L2
  is the IO coherence point), so no cache maintenance is needed, which is
  the same assumption the pool buffers already rest on. A non-linear skb
  is linearised by copying (it falls back to the copy path). It is a
  runtime parameter so the window can flip it after the copy path is seen
  working.
* **`deliver both` copies twice on receive**: the clone for the port netdev
  shares the data, but the stack's own copy (`tun`, AF_PACKET) still runs
  for `ffnpkt0`. It is an observation mode, not a destination.

## 3. The per-packet path, before and after

One transit packet, faceplate port A to faceplate port B, as the box runs
today (`ffn-portd` lanes on multi-queue TAPs, RPS on the TAPs, 8 driver
groups):

| step | thread (CPU)                      | copy of the frame            | syscall                 |
|------|-----------------------------------|------------------------------|-------------------------|
| 1    | PKI                                | DMA into a pool buffer (no CPU) | —                     |
| 2    | `ffn-packet-rx<g>` (CPU 1..8)      | 1: pool → skb (`skb_put_data`) | —                     |
| 3    | same, `netif_rx` → backlog softirq | AF_PACKET fanout `skb_clone` (no copy) | —             |
| 4    | ffn-portd lane rx worker           | 2: skb → user (`recvmmsg`)     | `recvmmsg` (1/64 amortised) |
| 5    | same                               | 3: user → new skb (`write(tap)`) | `write`, one per frame |
| 6    | inside that write, or an RPS CPU (24..39) | forward: VLAN, conntrack, nft, route; enqueue on the egress TAP (no copy) | — |
| 7    | ffn-portd lane tx worker of B      | 4: skb → user (`read(tap)`)    | `read`, one per frame   |
| 8    | same                               | 5: user → new skb (`sendmmsg`) | `sendmmsg` (1/64 amortised) |
| 9    | same, `dev_queue_xmit` → `trunk_xmit` | 6: skb → pool buffer (`skb_copy_bits`) | —            |
| 10   | PKO                                | DMA from the pool buffer       | —                       |

Six memcpy of the frame, four of them across the user/kernel boundary, two
unamortised syscalls per frame plus two amortised, three threads of three
processes' worth of scheduling (the driver thread, the rx lane, the tx
lane), and one DQ for all 40 cores. Measured: **~64k cycles per packet**,
three cores busy at 75 kpps / 900 Mbit/s (2026-10-21; `octeon/debian/
PACKET-PERFORMANCE.md` has the earlier steps of that history).

After, with `port_netdevs=1`, `deliver ports`, `tx_queues=8`:

| step | thread (CPU)                   | copy                                | syscall |
|------|--------------------------------|-------------------------------------|---------|
| 1    | PKI                             | DMA into a pool buffer              | —       |
| 2    | `ffn-packet-rx<g>`              | 1: pool → skb; envelope parsed, `eth_type_trans` | — |
| 3    | same `netif_rx`, or an RPS CPU  | forward: conntrack, nft, route, `dev_queue_xmit(ffnpB)` | — |
| 4    | same CPU, `port_xmit` on DQ `cpu % 8` | 2: skb → pool buffer (or 0 with `tx_gather=1`, envelope only) | — |
| 5    | PKO                             | DMA                                 | —       |

Two copies (one with gather), no syscalls, one or two threads, eight DQs.

Cycle estimate for the new path, labelled as such (nothing below is
measured yet; the baseline is):

| piece                                                   | cycles  | basis                                   |
|---------------------------------------------------------|---------|-----------------------------------------|
| driver receive: `GET_WORK`, chain walk, exact membership, skb alloc + copy, free | ~15k | measured share of the driver thread at the 2026-10-21 baseline; `trunk_buffer()` is a linear scan over 512 entries called 2-3 times per packet, 1-2k of it |
| `eth_type_trans`, `netif_rx`, backlog, (RPS IPI)         | ~2k     | Linux typical at this clock              |
| IPv4 forward with conntrack, nft policy/NAT, route, neighbour | ~10-14k | Linux typical for a stateful forward on an in-order 1.6 GHz core |
| `port_xmit`: envelope, copy, descriptor, LMTDMA, reap     | ~4-6k   | the copy is 1500 bytes; LMTDMA round trip is ~1k |
| **total**                                                | **~31-37k** | against 64k today: 1.7-2.1× per packet   |

At ~35k cycles a core does ~45 kpps; 8 driver cores plus 8 RPS cores give
~0.7 Mpps, i.e. **~8-9 Gbit/s at 1500 bytes, ~4 Gbit/s at 800**, which is
the "~10G class, large packets, no inspection" tier the architecture note
assigned to the kernel path. The two clear wins still on the table inside
this path are the membership table (index by address, −1-2k) and receive
zero copy (−1k and the cache traffic of the copy), both phase 2. Inspection
is bytes-bound and does not belong on this path at all.

## 4. The link budget, and ports 25/26

One 40G XLAUI carries everything between the switch and the DP:
**inspected transit is capped at 40 Gbit/s per direction of that link**,
whichever software path carries it (17 Gbit/s inspected both ways = 34
Gbit/s each way, fits; a 40G full-duplex Internet circuit does not, which is
why offloaded sessions go FE100 → port 20 → port 3 and never touch the DP).

Read-only check, 2026-10-21 14:21 UTC, no writes:

* Switch side (`port.list` through the CP's BCM service):
  `xl24(24) up 40G XLAUI` and **`xl25(25) down 40G XLAUI`, `xl26(26) down 40G
  XLAUI`, both `enabled`**: the switch has the ports configured and enabled
  and is waiting for a link partner. `xl2(2)` reads up only because it is
  the health loopback (`BCM88375-PORTS.md`).
* DP side: `ffn_dp_link` shows BGX2/LMAC0 up with block lock, `lmac_type 4`
  (XLAUI), `pknd 8`; the kernel's BGX bring-up enabled "interface 2 (1
  LMAC)"; the SDK's own interface enumeration from the userspace forwarder
  (`DP-FORWARDER.md`) lists **interfaces 0, 1, 3, 4, 5, 6 as DISABLED**.
  That enumeration is read from the QLM/GSER configuration, so the lanes
  that would face xl25 and xl26 (switch quads 13 and 10, `BCM88375-
  PORTS.md`) are not configured as XLAUI on the DP today. Whether they are
  physically wired on this SKU is unknown: bringing them up means a GSER
  lane-mode and BGX LMAC configuration on the DP, which is a write, plus a
  second PKND/channel set in this driver. The vendor's tables
  (`dp_port_list = {24, 25, 26}`, both DP0/DP1 labels) say the board has
  the traces. No read-only tool on the DP exposes the GSER lane modes;
  adding `GSERX_CFG`/`GSERX_LANE_MODE` snapshots to `ffn_dp_packet_probe`
  would settle "wired or not" without a write, and is a one-step follow-up.
* The DP boots with `pktbuf=8192,2048 wqe=256,128` on its command line (the
  bootmem the SDK runtime expects); this driver uses its own pools.

So: 40 Gbit/s per direction is the budget of the kernel path and of the
future userspace path together, and a second link is a separate project.

## 5. Migration plan: phase 2

What retires, and what takes its place:

| today                                                   | phase 2                                                                   |
|---------------------------------------------------------|---------------------------------------------------------------------------|
| core `ffn_linux_network.py` creates TAPs `pN` in `ffn-data` (`ip tuntap add … multi_queue`) and remakes plain ones | `--backend native` already exists ("must already be provisioned in the data namespace"): it configures devices it finds. The owner moves `ffnpN` into `ffn-data` as `pN` (`ip link set ffnpN netns ffn-data name pN`) at boot, before `start`; `remember_tap_mac` becomes `port <n> mac` on the control file |
| ffn-portd lanes: AF_PACKET fanout on `ffnpkt0`, `write`/`read` on TAP queues, `packet-lanes.json`, worker CPUs from `ffn_packet_cpus.py`, `ffn_tap_steering.py` RPS masks | ffn-portd becomes **control only**: LACP on the member netdevs (`ffnp21`/`ffnp22`, AF_PACKET as the control socket is today), `lag <id> collect/distribute/hardware` and `carrier` written to the control file from the engine's gates and the CP's link observation, FE100 returns from `ffnret0` (decoder unchanged), steering = `steer app` when the dataplane takes over. `packet-lanes.json`, the lane CPU reservations and `ffn_tap_steering` retire; RPS, if wanted, is set once on `ffnpN`'s `rx-0..7` queues by the network owner |
| `deliver both`                                           | `deliver ports`; `ffnpkt0` keeps only unmapped sources and the raw path for commissioning tools |
| the security runtime's `iif <index>` rules on TAPs        | unchanged: the netdevs are ordinary Ethernet devices in `ffn-data`; its drift check replays on index change as it does for a remade TAP |
| trunk driver copies on receive                            | refillable page-backed aura for data buffers, `build_skb` receive (DMA-layer change, tested on the box) |
| 512 pool-61 buffers for everything                        | the application's data aura sized for its backlog; aura 1021 stays the punt aura |

Order: (1) this module with `port_netdevs=1 deliver both`, observed; (2)
`tx_queues=8`; (3) ffn-portd control-only on a branch, tested in its
namespace harness (`test-portd-linux.py`) with `ffnp` devices faked by
veths; (4) the core owner's `--backend native` path in use; (5) `deliver
ports`, lanes retired; (6) the dataplane's `steer app`.

## 6. Rollback

* Runtime: `echo "deliver trunk" > /sys/kernel/debug/ffn_dp_packet_init/control`
  stops every port delivery at once; `ffnpkt0` carries everything as before.
* Boot: `port_netdevs=0` (and `tx_queues=1`) in the modprobe options, DP
  restart: the module then registers `ffnpkt0` only and behaves as `c7bf7034`.
* Binary: put the previous `.ko` back (the NFS root keeps `.bak-20261021`),
  DP restart.

The module is load-once per boot (DMA pinned); every change of parameter or
binary is a DP restart through the plane lifecycle, in the owner's window.

## 7. Deployment window (not done; the owner runs it)

1. On the MP, keep the running module and install the new one into the DP's
   NFS root (live on the DP at once, loaded at the next boot):
   `cp .../extra/ffn_dp_packet_init.ko .../extra/ffn_dp_packet_init.ko.bak-20261021b`,
   then copy the built `.ko` (md5 in the PR) over
   `/opt/ffn-images/2026.10.15.1/dp/rootfs/lib/modules/6.18.49-ffn-dp-e4bfcbb72e20/extra/ffn_dp_packet_init.ko`.
2. Parameters, in the same root:
   `etc/modprobe.d/ffn-dp-trunk.conf` with
   `options ffn_dp_packet_init port_netdevs=1` (first window; `tx_queues=8`
   is the second flip, `tx_gather=1` the third, each its own restart).
   `ffn_dp_packet_init.py reconcile` runs `modprobe`, which reads it.
3. DP restart through the plane lifecycle (the restart used on 2026-10-21
   04:03; the lifecycle re-stages packet init, pushes the port table and
   replays the policy; about two minutes).
4. Verify, read-only, on the DP:
   `cat /sys/kernel/debug/ffn_dp_packet_init/status` →
   `"port_netdevs":1`, `"deliver":"both"`, `"steer":"kernel"`, `rx` rising
   with `wire_rx`, `group_mismatch 0`, `error 0`;
   `cat /sys/kernel/debug/ffn_dp_packet_init/ports` → `ffnp5`, `ffnp1`,
   `ffnp21`/`ffnp22` with `rx_down` rising (demultiplexed, devices down),
   `ffnret0` `rx_down` rising while sessions are offloaded, `unmapped` 0;
   `ip -s link show ffnpkt0` unchanged in behaviour; ffn-portd's rows
   `running` with their lanes; a download through the box at the usual
   rate. Do **not** bring a port netdev up on a port the TAP path serves
   while `deliver both` (two stacks would answer ARP); an unused port
   (`ffnp3`) may be brought up with IPv6 disabled to tcpdump it.
5. Rollback as in §6.

## 8. Files

* `octeon/kctl/ffn_dp_port.h` — pure helpers: envelope parse/build, system
  port table, group/queue selection, LAG key and member choice, the LMTDMA
  word, the WQE view and admission, the control grammar.
* `octeon/kctl/ffn_dp_trunk.h` — the driver: groups and steering, descriptor
  queues, receive demultiplexing, port/LAG/return netdevs, control apply,
  `status` and `ports`.
* `octeon/kctl/ffn_dp_dma.h` — pools, SSO, PKO memory and the queue topology
  (`tx_queues`).
* `octeon/kctl/ffn_dp_packet_init.c` — the module: `prepare`, `control`,
  `ports`, `status` under `/sys/kernel/debug/ffn_dp_packet_init/`.
* `octeon/kctl/test_dp_port.c` — the host unit test (`make test-host`; CI
  runs it on x86 and big-endian under qemu-mips64).
* `octeon/debian/build-dp-packet-init.sh` and `octeon/images/build_images.py`
  — build the module for an image (every `.c`/`.h` of `kctl` is copied;
  the test file is not in `obj-m`).
