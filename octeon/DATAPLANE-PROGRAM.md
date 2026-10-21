# The dataplane program: from 1 Gbit/s in the kernel to 17 Gbit/s inspected and 40 Gbit/s forwarded

**Status 2026-10-21: design and phased plan. No code, nothing deployed.** Written
against `main` at 8cf3ece (PR #96 merged), platform PR #102 (the CP admission
loop, `fe100/ADMISSION-LOOP.md`), the sibling proposal to make the kernel trunk
driver the shared PKI/SSO/PKO owner (branch `codex/dp-trunk-shared-owner`, in
progress), and a read-only look at the appliance on 2026-10-21 14:19 UTC.

Targets set by the owner on 2026-10-21: scale to a 40 Gbit/s Internet circuit;
about 17 Gbit/s with inline inspection (the vendor's App-ID figure for this
model; its full-threat figure is about 9 Gbit/s); with inspection off, as close
to line rate as possible. Standing rules apply throughout: every dataplane
component is C (Python only for MP/CP glue and tests); own code and open content
only, no vendor binaries or signatures packaged; the appliance is in production,
so every phase below is a maintenance-window change with a rollback.

Where a number is measured it says where; where it is an estimate it says so and
shows the arithmetic. Section 7 lists the read-only checks that would turn the
largest estimates into measurements before any code is written.

---

## 0. Where the box stands (read-only, 2026-10-21 14:19 UTC)

| fact | value | source |
|---|---|---|
| dataplane processor | `CN7885p2.0-1600-AAP`, OCTEON III, 40 cores, 1600 MHz, 32 GiB | DP `/proc/cpuinfo`, `/proc/meminfo` |
| DP kernel | `6.18.49-ffn-dp-e4bfcbb72e20`, root over NFS from the MP (`127.1.1.1:/opt/ffn-images/2026.10.15.1/dp/rootfs`) | `uname -r`, `/proc/mounts` |
| packet path | kernel trunk driver `ffnpkt0`: 8 SSO groups on CPUs 1..8, one PKO descriptor queue, PKI pkind 8 / styles 8..11, packet aura 1021 (pool 61: 512 blocks of 4 KiB) | `/sys/kernel/debug/ffn_dp_packet_init/status` |
| trunk counters since the 04:03 UTC boot | rx 32.7 M frames, tx 30.9 M, 12 rx drops, 44 tx rejected, mean frame 893 B | same |
| forwarding engine | Linux in the `ffn-data` namespace: `ffn_security` (chains `forward`, `r0..r5`, `implicit_allow`, `grant`), `ffn_nat` (`prerouting`, `r0..r5`), `ffn_policy_gate`, ten `ffn_ifmgmt_*` input tables; 348-line ruleset, 171 rule lines | `nft list ruleset` in `ffn-data` |
| sessions | conntrack 2,679 of 262,144; TCP established timeout 432,000 s; UDP stream 120 s | `sysctl` in `ffn-data` |
| ports | p1 (wan1, BCM 28, 2 lanes), p2 (BCM 13, 2 lanes), p5 (BCM 16, 4 lanes), ae1 on 21/22 with VLAN units 69/17/101/447/375/444; p3,4,6..24 disabled | `/run/ffn-portd.json`, `ip -n ffn-data link` |
| session feed | observation-only, relay rebuilding a snapshot at the time of the read; 150 software / 146 L3 candidates; stream capacity 8192 rows | `/run/ffn-fe100-session-inventory.json` |
| FE100 | CP control owner in `observe` admission mode (PR #102 deployed 2026-10-21), 0 hardware sessions; return bindings held by `ffn-portd` (`/etc/ffn/fe100-return.json`: trunk 24, return 20) | memory of the 05:xx deploy; `ffn-portd` rows |
| measured throughput | upload 903 Mbit/s aggregate / 854 single stream through the software path with a 1G client NIC; download source-bound at 705..780; zero in-box drops | `ffn-line-rate-program-20261020` (2026-10-21 04:40) |
| measured cost | about 64 k cycles per transit packet on the TAP path (three cores at 70..81 % for 75 kpps per direction); the kernel driver's receive alone about 15 k cycles per frame | 2026-10-20 measurements, same note |

Two things about today's path matter for everything below. Every transit
packet is copied four times and crosses the kernel/user boundary four times
(driver `netif_rx` to AF_PACKET lane, lane `write(tap)`, kernel forward inside
that write, egress lane `read(tap)`, `sendmmsg` back to the trunk). And every
forwarding decision is Linux conntrack plus nftables, which is correct and
complete but costs tens of thousands of cycles per packet on an in-order MIPS
core and cannot inspect payload at all; inspection today would be yet another
copy.

---

## 1. The physical budget

### 1.1 The hardware

| part | what it is | links that matter |
|---|---|---|
| DP: CN7885 (OCTEON III CN78XX pass 2.0), 40 cnMIPS III cores at 1600 MHz, 32 GiB | the only place with enough compute to inspect; `AAP` is Cavium's accelerator-complete variant (ZIP and the regex engine are expected present; neither has been probed, see 4.7) | **one** populated 40G XLAUI to the switch: BGX interface 2 = BCM port 24 (PKI channel 0xa00). BCM 25/26 (`nifdp1/2`) are in the vendor's `config.bcm` but the DP's BGX enumeration shows interfaces 0,1,3..6 disabled; one read-only check (section 7) settles whether they are wired |
| BCM88375 switch (Qumran-MX) | all 24 faceplate ports hang off it: 4 RJ45 (10GBASE-T PHY), 16 SFP+, 4 QSFP28 (CGE, 100G; 40G with QSFP+ optics) | port 3 = 100G CAUI through the Sesto gearbox into the FE100 NIF; port 20 = Interlaken 12 x 12.5G (150 Gbit/s raw) from the FE100 TMI back; ports 4/5 KR to the CP; 8/9 KR to the MP; 12 HSCI; 17 recycle |
| FE100 session-offload ASIC | per-session forward / rewrite / NAT / drop in hardware; misses, exceptions and TCP control segments return to the CPU path as 32-byte-header messages; pushes FLOWSTATS counter records | registers only on the CP's PCIe (BAR0, 1 MiB); packets only through the switch (3 in, 20 out) |
| CP: CN73XX, 8 cores | FE100 and BCM owner, Python control services; no Interlaken | 128 Mbit/s, 0.7..9 ms RTT to the DP over `ffn_dpnet` |
| MP: Xeon D-1548 | management, policy compiler, image/NFS server for both planes | KR to the switch; not a data path |
| CA1/CE40 socket (`feed:a101` / `feed:a00d`) | FPGA Aho-Corasick/DFA matcher, fed from the DP over its own Interlaken (GSER5, 4 lanes at 6.25 Gbaud = 25 Gbit/s raw) | that link never came up on the vendor's own boot ("ILK enable failed"); registers mapped, semantics partly recovered, DDR training gate never observed set (`octeon/ca1/`, `octeon/nac/`) |

### 1.2 The four paths

| path | what it is | links | ceiling | can inspect | status |
|---|---|---|---|---|---|
| **A. TAP kernel path** (today) | kernel trunk driver, AF_PACKET lanes into multi-queue TAPs, Linux conntrack/nft/NAT/route, lanes back out | the one 40G XLAUI, shared by both directions | **measured** about 0.9 Gbit/s per direction at the 1G client; **estimated** 1.0 Mpps = 12 Gbit/s at 1518 B / 3 Gbit/s IMIX if all 40 cores could run it at 64 k cycles (they cannot: the kernel needs cores, and the TAP queue per lane and the single PKO DQ lock serialize) | no (any payload scan is a fifth copy) | in production |
| **B. per-port kernel netdevs** (sibling proposal) | trunk driver owns PKI/SSO/PKO and exposes one netdev per faceplate port; Linux forwards natively, no TAPs, no AF_PACKET copies | same 40G XLAUI | **estimated** 15..25 k cycles/packet: 10 Gbit/s class at 1518 B on 8..16 cores, up to 30 Gbit/s with most of the chip; 3..9 Gbit/s IMIX; a single PKO DQ caps it at about 8 Gbit/s unless per-core DQs are added | no | design in progress |
| **C. FE100 offload** | sessions installed in the ASIC; the DP sees only the first packets, exceptions and FIN/RST | 100G in (port 3), 150G raw out (port 20); the family's top model is rated 68 Gbit/s on this chip, so it is not the 40G limit | **link-bound for a 40G full-duplex circuit** (80 Gbit/s of transit fits in 100G); session capacity about 4 million per the vendor sheet (not measured; FLU initialised at "capacity 4") | no (forward, rewrite, drop only) | proven forward + rewrite + NAT + drop + hand-back on real sessions; admission in observe mode |
| **D. userspace dataplane** (`octeon/dpfwd`) | run-to-completion forwarder on N cores driving PKI/SSO/PKO3 directly, with a session table, policy, NAT and inspection in C | same 40G XLAUI | **estimated** 2..4 k cycles/packet on a session hit: link-bound at 1518 B with 8 cores; IMIX at 40G needs 13.1 Mpps = all 32 dataplane cores at <= 3.9 k cycles | **yes**: this is the only path that can | about 14 k lines incl. tests; builds and initialises on the CN78XX; no packet has yet crossed PKO3 on silicon; no session table, NAT or TCP state |

### 1.3 Cycle budgets

Derivation. The chip offers 40 cores x 1.6 GHz = **64.0 Gcycles/s**; a design
that keeps 8 cores for the kernel, control and exceptions leaves **51.2
Gcycles/s** for the dataplane. A frame of F bytes costs F + 20 bytes on the
wire (preamble and inter-frame gap), so the packet rate at R bit/s is
R / (8 (F + 20)). "Simple IMIX" is 7:4:1 of 64/594/1518-byte frames, mean 362 B.
Cycles per packet available = cycles per second / packets per second.

| rate | frames | Mpps | cycles/packet, 40 cores | cycles/packet, 32 cores | cycles/packet, 16 cores |
|---|---|---:|---:|---:|---:|
| 1 Gbit/s (today's circuit) | 1518 B | 0.081 | 787,000 | 630,000 | 315,000 |
| 9 Gbit/s | 1518 B | 0.731 | 87,500 | 70,000 | 35,000 |
| 9 Gbit/s | IMIX 362 B | 2.945 | 21,700 | 17,400 | 8,700 |
| 17 Gbit/s | 1518 B | 1.382 | 46,300 | 37,100 | 18,500 |
| 17 Gbit/s | IMIX 362 B | 5.563 | 11,500 | 9,200 | 4,600 |
| 34 Gbit/s (17 G each way through the DP link) | 1518 B | 2.763 | 23,200 | 18,500 | 9,300 |
| 34 Gbit/s | IMIX 362 B | 11.126 | 5,750 | 4,600 | 2,300 |
| 40 Gbit/s (the DP link full) | 1518 B | 3.251 | 19,700 | 15,700 | 7,900 |
| 40 Gbit/s | IMIX 362 B | 13.089 | 4,890 | 3,910 | 1,960 |
| 80 Gbit/s (a 40G circuit, both directions) | 1518 B | 6.502 | 9,840 | 7,900 | 3,900 |
| 80 Gbit/s | IMIX 362 B | 26.178 | 2,450 | 1,960 | 980 |

Against the paths:

| path | cycles/packet | basis | what that buys on this chip |
|---|---|---|---|
| A. TAP path | **64 k measured** | 3 cores at 70..81 % carrying 75 kpps per direction (2026-10-20); 3 x 1.6e9 x 0.75 / 150e3 = 24 k per transit frame counted once per core, 64 k summed over the RX lane, the kernel forward inside `write(tap)` and the TX lane | 1.0 Mpps on all 40 cores = 12 Gbit/s at 1518 B, 3 Gbit/s IMIX, if it scaled perfectly; it does not scale past the lanes and the DQ lock, and it leaves nothing for the kernel's own work |
| B. kernel netdevs | **15..25 k estimated** | receive measured about 15 k incl. the AF_PACKET copy; a netdev receive without that copy about 8 k; conntrack + 171 nft rule lines + NAT + route on an in-order dual-issue core about 2.5..3x the 3..6 k cycles the same path costs on x86; PKO submit and completion about 2 k | 16 cores: 1.0..1.7 Mpps = 12..21 Gbit/s at 1518 B, 3..5 Gbit/s IMIX. 32 cores: 25..42 Gbit/s at 1518 B (link-bound), 6..10 Gbit/s IMIX. Each egress needs its own PKO DQ per CPU or the DQ spinlock caps the whole box near 0.6 Mpps = 8 Gbit/s at 1518 B |
| C. FE100 | 0 on the DP for a hit | the DP pays only the first packets (session creation) and FIN/RST | bounded by the install rate, not by packets: see 2.6 |
| D. userspace forwarder, session hit | **2..4 k estimated** | GET_WORK 200..500 (synchronous; IOBDMA prefetch hides most), header parse 300, symmetric hash + lookup 300..800 (one or two L2 misses at about 40 cycles each; a DRAM miss at about 160 cycles only when the entry is cold), TCP state + counters 200, NAT rewrite with incremental checksum 300, L2 rewrite 100, PKO3 LMTDMA submit 200..400. The forwarder's flow-cache lookup today probes <= 16 slots and measured 67 ns per miss on x86 at half load | 32 cores: 12.8..25.6 Mpps. 1518 B at 40G (3.25 Mpps) is link-bound with 8 cores; IMIX at 40G (13.1 Mpps) is reachable only at the low end of the estimate with all 32 cores; expect 25..35 Gbit/s IMIX |
| D. userspace forwarder, session creation | **10..20 k estimated** | two LPM lookups (33 hashed probes each in `dp_l3`), DNAT then security policy match (today a linear scan over the rule table: 171 rules x about 15 cycles; must become a decision tree before rule counts grow), SNAT port allocation from a per-core block, two session keys inserted | 169 k sessions/s (the vendor's figure) x 20 k = 3.4 Gcycles/s = 2 cores |

### 1.4 Inspection budgets, cycles per byte

Derivation. Payload is about 1460 of every 1538 wire bytes at 1518 B, so
R bit/s carries R / 8 x 0.949 bytes/s of payload. Cycles per byte available =
cycles per second / payload bytes per second.

| rate | payload | cycles/byte, 40 cores | 32 cores | 24 cores | 16 cores |
|---|---|---:|---:|---:|---:|
| 9 Gbit/s | 1.07 GB/s | 60 | 48 | 36 | 24 |
| 17 Gbit/s | 2.02 GB/s | 32 | 25 | 19 | 13 |
| 40 Gbit/s | 4.75 GB/s | 13.5 | 10.8 | 8.1 | 5.4 |

Realistic software costs on this core (estimates; the forwarder's own
Aho-Corasick is the measuring stick):

| matcher | cycles/byte | why | what it buys |
|---|---:|---|---|
| `dpfwd` Aho-Corasick as written (`ffn_dp_aho.c`: full 256-way table, 1 KiB per state, cap 65,536 states = 64 MiB) | 100..200 when the automaton exceeds the 16 MiB shared L2 | one dependent load per byte; a DRAM miss on this part is about 100 ns = 160 cycles | 40 cores scan 3 Gbit/s. A correctness-first implementation; not the production matcher for large sets |
| compact Aho-Corasick (bitmapped or compressed transitions, 32..128 B per state; 50 k states in 1.6..6.4 MiB) | 20..40 | L2-resident: an L2 hit about 40 cycles plus loop overhead | 24 cores scan 8..15 Gbit/s; 32 cores 13..26 Gbit/s |
| compressed DFA for application signatures (hot states L1-resident) | 10..15 | one table lookup per byte, small alphabet classes | 16 cores scan 14..20 Gbit/s |
| prefilter (shift-or / Bloom over 4-byte grams, Wu-Manber style) | 4..6 | rejects >= 95 % of windows before the full automaton runs | 24 cores pass 60 Gbit/s; makes the full scan run on a few percent of bytes |
| hardware matcher (CA1/CE40, or the OCTEON's own regex engine if present) | about 0 on the cores | | 4.7 |

Consequences. The 17 Gbit/s class is cheap because App-ID scans **flow heads,
not streams**: at the vendor's 169 k new sessions/s and 4 KiB scanned per flow
(the forwarder's existing bound of 8 payload packets x 2 KiB is 16 KiB), that is
692 MB/s of scanned bytes = 6.5 cores at 15 cycles/byte. The 9 Gbit/s class
scans **every cleartext byte**: at 1.07 GB/s it needs 24 cores at 36
cycles/byte, which is only reachable with a compact L2-resident automaton and a
prefilter in front of it. Most Internet payload is TLS; without TLS inspection
(out of scope here) the full-stream tier applies to the cleartext fraction and
to protocols the box terminates.

### 1.5 Which targets are reachable on which path

| target | A. TAP | B. kernel netdevs | C. FE100 | D. userspace dataplane |
|---|---|---|---|---|
| 1 Gbit/s circuit, today | yes (measured) | yes | yes | yes |
| 10 Gbit/s, no inspection | no | **yes at 1518 B** (8..16 cores), marginal at IMIX | yes with C handling the sessions B creates | yes |
| 40 Gbit/s one way through the DP, no inspection | no | marginal (needs most cores, per-core DQs, 1518 B only) | **yes** | **yes at 1518 B; 25..35 Gbit/s IMIX** |
| 40 Gbit/s circuit full duplex (80 Gbit/s transit), no inspection | no | no (the DP link is 40G) | **yes, the only path** | no through the DP link; yes as the admission engine in front of C |
| 17 Gbit/s with App-ID | no | no | no | **yes**: 34 Gbit/s of transit on the 40G link, flow heads scanned on <= 8 cores, long flows handed to C after identification |
| 9 Gbit/s full inspection (cleartext) | no | no | no | **yes with a compact automaton + prefilter on about 24 cores**; not with the current full-table matcher |
| 169 k new sessions/s | no (Python admission, conntrack events at about 25/s to the feed) | no (conntrack creates them; admission to C through the CP stays in the hundreds per second) | needs an installer that fast | **yes**: 2 cores of creation cost plus a microsecond install path into C (2.6) |

### 1.6 Where the single 40G DP link caps inspected traffic

The XLAUI is full duplex, but **both directions of a transit flow arrive on its
receive side**: LAN to WAN enters the DP from the switch and so does WAN to LAN.
So the DP can inspect at most **40 Gbit/s of total transit**, less the return
messages, punts and exceptions that share the link. 17 Gbit/s inspected each
way is 34 Gbit/s of transit and fits with 6 Gbit/s of headroom; a 40 Gbit/s
circuit used both ways (80 Gbit/s) does not fit through the DP at all, which is
why the FE100 is the forwarding path at that scale and the DP is the admission
and inspection engine in front of it. If BCM ports 25 and 26 turn out to be wired
to the DP (section 7), a three-link LAG raises the DP cap to 120 Gbit/s; nothing
in this document assumes it.

---

## 2. The session model

### 2.1 Who owns sessions

The DP userspace dataplane owns every data session, in C, from its first packet
to its close. The kernel keeps conntrack for the traffic the kernel still
terminates or forwards (2.4) and nothing else; the CP stops being a
decision-maker for admission and becomes the FE100's table operator and the
auditor of what the DP decides (2.6, 2.7).

The table is one shared, cache-line-aligned open-addressing table in
hugepage-backed shared memory (a CVMX named block, so every core maps the same
physical pages), sized for the vendor's 4 million sessions at 128 bytes per
entry = 512 MiB plus two 8-byte key slots per session in a separate index (the
original and the reply key both point at one entry, so the reply direction hits
without a second lookup structure). Entries are allocated from **per-core free
lists** and aged by **the core that created them** (each core sweeps its own
creations incrementally, `DP_FLOW_SWEEP_SLOTS` at a time, as the flow cache
does today), so creation and aging need no locks. The forwarder's existing flow
cache (`struct dp_flow_ent`, 48 bytes, bounded probe, stalest-eviction, idle
sweep) is the seed of this table; it grows the TCP state machine, both tuples
(pre- and post-NAT), per-direction counters, the rule and NAT decision, the
inspection state (scan position per direction, application id, verdict), the
offload state (flow ids, install state, last FLOWSTATS activity) and the
timers.

### 2.2 Steering both directions to one owner, and why it is the first design decision

PKI hashes a frame into an SSO group from a byte window over the addresses and
ports (`ffn_dp_trunk.h`: `trunk_tag_configure`, windows 26..37 / 30..41 /
28..43). That CRC is **not symmetric**: the two directions of one session land
in different groups with probability (G - 1) / G for G groups, so a strict
"each core owns the sessions its group delivers" sharding would have to hand
about half the packets of one direction to another core. The OCTEON idiom for
that hop is a tag switch with deschedule (`cvmx_pow_tag_sw_desched` into the
owner's group), which costs one SSO round trip, roughly a microsecond of latency
and a few hundred cycles, on 47 % of all packets at G = 32. The vendor's
exported names (`pan_sesscache_get_hosting_cpu`) show it chose a hosting core
and paid for it.

**Decision: serialize by SSO atomic tag instead of by core.** The receiving core
computes the symmetric session hash (it has parsed the header anyway) and
switches the work's tag to ATOMIC with that hash. The SSO then guarantees that
no two cores hold work with the same atomic tag at the same time and that work
with the same tag is delivered in arrival order, which is exactly the per-session
mutual exclusion and ordering a stateful firewall needs, with no lock and no
hop. The session entry's cache lines move between cores only when the two
directions do land on different cores, and on this part that is an L2 transfer
(the L1 data cache is write-through, the 16 MiB L2 is the coherence point),
about 40..60 cycles per line, cheaper than a deschedule. Per-core ownership
stays where it is free: allocation, aging and counters. The forwarder already
carries the tag (`oct_wqe.flow_tag`) and an `ordered_egress` tag-switch option
on transmit; the atomic switch belongs right after `dp_parse()` in the new
session stage. Phase 3's A/B test (5) measures both models on the lab pair
before the choice is final.

### 2.3 Session creation on the first packet

1. **Parse** (`dp_parse`, extended to IPv6 and to the FE100 return envelope):
   ingress logical port and VLAN from the OTMH/ITMH envelope and tag; 5-tuple;
   TCP flags; payload offset.
2. **Ingress zone** from the attachment table (logical port + VLAN unit -> zone,
   vsys), the same intent the MP compiles today for the FE100 attachment owner
   (`management/fe100_attachment_config.py`) and that `ffn_security_runtime`
   expresses as `iif <index>` matches.
3. **Lookup**: symmetric hash, atomic tag switch, probe. A hit is the fast path.
4. **Miss, TCP without SYN**: policy decides (default drop; the PAN-OS
   "tcp-reject-non-syn" behaviour) so a stale flow never creates a session.
5. **Destination NAT** policy on the original tuple; the to-zone for NAT
   matching comes from a route lookup on the original destination.
6. **Route** the (post-DNAT) destination in the dataplane's FIB (`dp_l3`,
   longest-prefix match, ECMP) -> egress logical port, next hop, egress zone.
   No neighbour yet means punt to the kernel (2.4) and keep the session in
   `pending-neighbour`; the kernel's ARP/ND reply reaches the dataplane through
   the netlink mirror within milliseconds.
7. **Security policy** in C on pre-NAT addresses with post-NAT zones: from-zone
   set, to-zone set, source and destination address sets, service, user/app
   placeholders, action, inspection profile, log flag. This is the PAN-OS rule
   order the MP already compiles (`ffn_policy_plan.py` -> nft today), rendered
   additionally into the dataplane's policy bank (`policy.bin`, loaded with
   `dp_tables_load` into the inactive bank, activated with `dp_activate_bank`).
   The linear `dp_classify` is replaced by a compiled decision tree or
   tuple-space search before rule counts exceed a few hundred.
8. **Source NAT** selection (dynamic IP and port from a per-core slice of the
   pool's port range, so no allocator lock; static and DNAT-only as the rules
   say).
9. **Create** the entry with both keys, the TCP state (SYN_SENT), timers, the
   decision, the rewrite template per direction (MACs, VLAN, TTL decrement,
   NAT fields, incremental checksum deltas), and the inspection and offload
   state. Log the session start into the per-core event ring the exporter
   drains (2.7).
10. **Forward** this packet through the same hit path.

Timers, configurable, PAN-OS defaults: TCP established 3600 s (the FE100 aging
module already uses it; conntrack's 432,000 s today is a Linux default and is
not kept), TCP handshake 10 s, TCP half-closed 120 s, TIME_WAIT 15 s, UDP 30 s,
ICMP 6 s, other IP 30 s. Aging is the creator core's incremental sweep plus the
FLOWSTATS-driven activity for offloaded sessions (2.6).

### 2.4 Policy evaluation in C; what the kernel keeps

What replaces nftables for data traffic is the pipeline above: the zone and
rule semantics the MP compiles are the same, the match is a compiled table
instead of chains `r0..r5`, and NAT is a decision stored on the session rather
than a per-packet chain. What today's ruleset also carries, and the new path
keeps with the same meaning:

| nft construct today (`FFN-NGFW/opt/ffn_security_nft.py`, `ffn_security_runtime.py`, `ffn_nat_runtime.py`) | kept as |
|---|---|
| `ffn_security` `forward` (priority -260, policy drop): preamble `meta nfproto != ipv4 drop`, `ct state invalid drop`, reply direction only when established and granted; then per rule, per zone pair, per direction `iif {ifindexes} oif {ifindexes} ct original ip saddr/daddr {sets} [l4proto, ports] goto r<i>`; `r<i>` counts and grants or drops; `implicit_allow` for intra-zone; final `counter drop` | the compiled rule table, in order, with the same zone-pair, address-set (<= 256 entries per set today) and service semantics, intra-zone implicit allow and the interzone default deny as the final rows. **IPv6 transit is dropped by today's preamble**; the new table is dual-stack from the start and enabling IPv6 forwarding becomes a policy decision |
| `grant` chain: `ct label` bit 0 = granted, bits 1..30 = the rule's token (`revision * 2048 + counter`), packet mark bit 31 = PERMIT; the session collector (`ffn_session_events.py`, netlink groups NEW/UPDATE/DESTROY) journals start/end records with counters into `/var/lib/ffn/security-sessions.db`, and the feed reports the token as `token`/`rule` | the session entry's rule id and generation; the exporter ring carries session start, end and periodic counter events with the same record schema (identity, token, started, ended, duration, counters), so session logging keeps its journal and the feed its fields |
| what the renderer refuses today: `application`, `source-user`, device matches ("requires an identity/inspection provider"), `application-default` services, profiles other than `none`, `reject`/`reset` actions ("requires a commissioned response provider") | the new table **adds** them: application and user are match fields filled by tier 1 (4.2) and a future identity source, profiles select the inspection tier, reject/reset are verdicts the dataplane executes (TCP RST both ways, ICMP unreachable through the kernel's ICMP generator) |
| platform guards `ffn_aggregate_<aeN>` (priority -250: drop anything on an aggregate without the PERMIT mark) | not needed: there is one forwarding owner; the aggregate's member gates stay in `ffn-portd` |
| `ffn_nat` `prerouting` (dstnat) / `postrouting` (srcnat), `ct mark = 0xf1000000 + revision * 1024 + rule` | the NAT decision at creation, stored per session (2.5) |
| `ffn_policy_gate` (priority -270, policy drop, `set live` with an **8-second timeout** the supervisor renews each tick: fail-closed when the policy owner, the collector or the session journal is unhealthy) | **not kept as a per-packet gate**: a loaded policy bank stays valid until replaced or explicitly withdrawn, and the control plane's absence must not black-hole traffic (the 2026-10-14 stalls were this gate expiring behind `/run/ffn-network.lock`). The property the gate protected, that no session is forwarded unjournaled, is kept differently: the start event is written to the core's ring before the first packet leaves; a full ring is counted and alerted, and whether new sessions then wait or proceed is a configured policy, default proceed |
| a replay (`drift()` sees a binding or ruleset change in three reads 0.5 s apart) closes the gate, re-renders and reloads everything: about 3 s of transit closure, conntrack not flushed | a policy or binding change is a bank build on the MP and a bank flip on the DP (4.5): no closure |
| `ffn_ifmgmt_<iface>` input tables (interface management profiles, priority -10: ping, ssh, https 443/8443, snmp, response pages, user-id, Protect 443/4501, DHCP client and ND always) and `ffn_interface_services.py`, which relays those services over a Unix socket to the MP | stay in the kernel unchanged; the dataplane's local-delivery check (destination is one of the box's own addresses, mirrored from the kernel) punts the packet into the kernel's per-port netdev, where these tables still apply |
| conntrack as the session table (no ALG helpers are built into the DP kernel today) | the dataplane table for transit; conntrack only for what the kernel terminates or forwards |

The kernel keeps, unchanged, everything that is not transit forwarding:
management access (SSH, HTTPS, the plane control sockets, the interface
management profiles and their relay to the MP), the FFN Protect VPN gateway
(`ffn_protect_gateway.py` plus the C worker `ffn-protect-tunnel`, which owns the
`tunnel.N` TUN device and terminates ESP-in-UDP 4501 and the SSL tunnel; its
decrypted inner packets enter the dataplane through the inject path in 3.6),
routing (today `ffn_static_routes.py` with its path monitors and the tracked
default routes, VRF tables and ECMP routes in the network owner; FRR 10.7.1 is
built into the DP image under `/usr/local/ffn-router` but not enabled; VRRP is
planned, not active), the service routes (`127.1.x` through the `svcrt0/1`
veth pair into `ffn-data`), ARP and IPv6 ND (the kernel's neighbour tables are
the source of truth; the dataplane mirrors them and never answers ARP itself,
retiring `dp_arp` on this platform), ICMP generation (TTL exceeded,
unreachable, fragmentation needed: the dataplane punts the triggering packet
with a reason code and the kernel builds the ICMP), the DHCP server (raw socket
per device), LLDP and LACP (both already on their own native receivers). The
dataplane classifies all of that as `FP_LOCAL_W` or an exception and hands it
over through the punt contract (section 3). IP fragments: reassembly stays in
the kernel; the dataplane forwards first fragments it can match and punts the
rest, as the forwarder's `DP_TF_FRAG` rule does today.

### 2.5 NAT in C

The NAT policy the MP compiles today (`ffn_nat_policy.py`, up to 1024 rules)
and renders into `ffn_nat` supports interface masquerade, dynamic-IP-and-port
to one address, static one-to-one source NAT, static destination NAT with an
optional port, and destination pools of up to 256 hosts selected round-robin
or by source or source+destination hash; it resolves the destination zone with
a FIB lookup on the original destination, which is why it refuses policy
routing, VRFs and ECMP; it rejects dynamic-IP without port, persistent
dynamic-IP-and-port, IP-modulo, least-sessions and NAT64/NPTv6. The same plan
is rendered into a second table in the policy bank: rule -> translation type
and pool. At session creation the translation is decided once and the entry
stores, per direction, the new source and destination address and port and the
16-bit checksum delta (RFC 1624, already in `dp_l3`'s rewrite). The per-packet
cost is the rewrite plus two incremental checksum updates. Port allocation is
per core from disjoint slices of the pool's port range (65,536 ports / 32 cores
= 2,048 per core per pool address; pools of many addresses multiply that); a
core that exhausts its slice borrows a block from a shared pool under a short
lock, which is rare. Persistent and round-robin dynamic IP, and the
destination-zone lookup through VRF and ECMP routes, become possible because
the lookup is the dataplane's own FIB rather than an nft `fib` expression.
ALGs (FTP, SIP, TFTP) exist nowhere today (the DP kernel builds no conntrack
helpers) and are not in the fast path: a rule that needs one marks the session
`kernel`, and the whole session is forwarded through the kernel path (3.6)
where a helper can be enabled.

### 2.6 Hand-off to the FE100

**Who decides, and when.** The DP decides admission at session creation
(allow, no inspection profile, TCP established or UDP after the first reply)
or at the point App-ID has identified the application and the rule allows it
(4.2). That replaces the CP loop's 5-second / 50-packet / 1-MiB threshold
(`ffn_fe100_offload_policy.should_offload`), which exists because the CP can
only see conntrack-derived rows minutes late; the DP sees every packet. The
non-negotiable gate (`admissible`: TCP or UDP, both directions seen, L3 plan
available, no inspection requirement) stays, as C.

**Install path and latency.** The FE100's tables are reachable two ways:

| path | latency | rate | status |
|---|---|---|---|
| CP PCIe (what every proof so far used): FLOWADD then FLOWUPDATE then a read-back through the pinned vendor library under one lock, each native call under a 10 s watchdog | milliseconds per session; the DP must ask the CP over `ffn_dpnet` (128 Mbit/s, 0.7..9 ms RTT) | hundreds to a few thousand per second with batching | proven; the CP control service owns it |
| in-band from the DP through the switch: a control message to the FE100 NIF (the vendor's message family has `SESS_SETUP 1 / SESS_UPDATE 2 / SESS_REMOVE 3 / SESS_AGEOUT 4` on the CONTROL type and a "bind/predict ack-nack" handler on the CP side, which says sessions were bound by message and acknowledged) | microseconds (one switch traversal each way) | tens of thousands per second per core | **not recovered**: the request format has to be read, as interface facts only, from the vendor's `pcs/packets/fe100.py` and the DP library's DWARF, then proven on the isolated port-13 loop |

The program needs both: the CP path is phase 1 and the fallback; the in-band
path is phase 4 and what makes 169 k sessions/s possible. Until phase 4 the DP
sends admission requests to the CP over a request ring, batched (up to 128 per
message, as the feed batches), and the CP control service installs them with
the owners it has (`PolicyOwner.admit_pair`, `PathOwner`, `AttachmentOwner`,
`FlowIds`); flow ids keep coming from the commissioned namespace
(`0x10000..0xFFFFFFFE`, per CP boot) until the in-band path moves allocation to
the DP with a range per core.

**What is installed.** Exactly what the first session used (run 8, 2026-10-19):
a session pair (64-byte entries: key `0x40, proto, zone, sport, dport, src,
dst`; flow id at bytes 36..40; action CT + next-hop override + TTL decrement,
or NAT mode 1/2 with the translated tuple at 48..60), two DIRECT next hops
(egress LIF, destination MAC, MTU, VLAN, source-MAC index), source MACs, and the
queue map whose **word 2 is the next-hop index** (runs 1..6 punted on exactly
that). The attachment-level entries (LIF per ingress port and VLAN with the zone
and miss next hop, LEF per egress port, egress rules on switch port 3 keyed on
the FE100's DSA bytes, force-forward of the front ports into port 3) are
per-port commissioning, not per session, and are phase 1.

**While the session is in hardware.** The DP sees nothing of it except what the
chip pushes: FLOWSTATS type 19 messages on port 20 -> 24 carrying 8-byte
records (`flow_id`, `reason[31:30]`, `packets[29:22]`, `octets[21:0]`, up to
125 per message, deltas). The dataplane consumes them in C (the decoder exists:
`octeon/native/ffn_fe100_stats.c`), credits the session's counters, and treats
a nonzero packet delta with reason != 2 as activity. Aging is the session's own
idle timer against that activity (TCP 3600 s, UDP 30 s): no activity report
within the timeout removes the hardware entry and the session. An unreported
flow is **unknown, never idle**: a receiver outage fences aging rather than
expiring the table, as the FE100 aging module already insists. `SESS_AGEOUT`
(code 4) is checked for in phase 4: if the chip ages flows itself and says so,
the DP's timer becomes a cross-check.

**Hand-back.** The chip punts a hardware session's TCP control segments to the
CPU path as type-2 messages carrying the flow id, the zone and the original,
unrewritten frame (measured: the client's FIN and its retransmissions, then
RSTs; data segments keep forwarding in hardware). The dataplane receives the
punt on its own groups (PKI style 10/11 marks returns), looks the session up by
flow id, runs the segment through its TCP state machine, removes the hardware
pair (FLOWDEL via the active install path), and forwards the segment and
everything after it in software until the session closes. The conntrack
`BE_LIBERAL` + timeout fix-up of the kernel era (`ffn_ctlease`, 432,000 s) is
not needed once conntrack is out of the transit path; until then (phases 1..2)
it is exactly what keeps a handed-back session alive. Misses (`FLOWUNKNOWN`,
type 8) and exceptions (TTL, MTU, first fragment, ARP/ICMP/ICMPv6/LLDP, LACP
`NOTFLOW`) arrive the same way and take the normal first-packet or punt path.

**Policy change.** A commit today drains every hardware session (the MP commit
barrier requires `drained()`). The dataplane re-evaluates its sessions against
the new bank incrementally (the sweep visits every entry within one aging
period) and withdraws from hardware only those whose rule or NAT decision
changed; until that is proven, the conservative drain stays.

### 2.7 How the CP and MP see sessions

The contract is kept and the producer is replaced. Today
`octeon/debian/ffn_session_stream_dp.py` reads conntrack over netlink, assesses
rows with `ffn_session_feed.assess`, and emits the stream protocol
(`management/session_stream.py`: `begin` with producer identity and limits,
64-row snapshot chunks, then ordered `changes` batches of at most 128 upserts
or closes, heartbeats every second, frames <= 512 KiB, inventory capacity 8192
rows, 10 s silence and 90 s backlog bounds, counter-only refresh every 10 s on
PR #102). The MP relay forwards each message to the CP control service in one
round trip (50..80 ms each, so 12..17 messages/s) and publishes
`/run/ffn-fe100-session-inventory.json` for the dashboard, the Session Browser,
`show session info|all|id` and the CP admission loop's `Observations`.

New producer: `ffn-dp-export`, a C sidecar on the DP that maps the session
table and the per-core event rings read-only and speaks the **same protocol
with the same field names** (`identity`, `original`, `reply`, `translated`,
`nat`, `counters`, `tcp_state`, `remaining_seconds`, `rule`, `token`,
`software_candidate`, `blockers`, `l3`, plus `hardware_admission` true for an
offloaded session, which today is always false). Identity stays the sha256 of
boot, zone, id and original tuple; the id is the dataplane's session id instead
of conntrack's. Relay, CP receiver, inventory file, dashboard, CLI and the CP
loop keep working unchanged; the only visible differences are that counters
move while a flow forwards (conntrack only reported them on events) and that
8192 rows is far too small for a 4-million-session table, so the exporter sends
a summary plus a bounded, filterable projection and the CP/MP views page
through it rather than holding the whole table. The CP admission loop keeps
running in `observe` mode as the auditor of the DP's decisions (what it would
have admitted versus what the DP did), which is what its shadow lease table is
for.

---

## 3. The punt contract with the kernel trunk driver

The sibling proposal makes `ffn_dp_trunk.h` the single owner of PKI, SSO and
PKO on the DP and gives the kernel one netdev per faceplate port. This section
fixes what the userspace dataplane needs from that owner so the two programs do
not diverge. Everything here is a proposal to be adopted by both; the exact
register and field choices belong to the driver and are referenced, not
duplicated.

### 3.1 Ownership

| resource | owner | how the application learns it |
|---|---|---|
| PKI: pkinds, styles, PCAM, QPG table, tag configuration, parser enable | **kernel**, exclusively; the application never writes PKI | `/dev/ffn-dp-packet` ioctl `FFN_DP_RESOURCES`: style and QPG numbers per class of traffic |
| FPA3: pools and auras, the packet buffer memory (hundreds of MiB, from a reserved region or CMA, not the 512 x 4 KiB pool of today) | **kernel** allocates and configures; the application maps the buffer region read-write | the same ioctl: aura numbers, physical base and size of every pool, buffer size, `first_skip` |
| SSO groups | **kernel** groups `K` (0..7 today) for its own receive; **application** groups `A` (8..39, one per dataplane core, or 8..23 with two cores each) | the ioctl; the QPG entries for data styles point at `A`, for control styles (3.4) at `K` |
| SSO core group masks (`SSO_PP(x)_S(y)_GRPMSK`) | the kernel sets them for its CPUs; the application sets its own cores' masks to `A` only, through the ioctl (not by direct CSR write, so the kernel can audit) | |
| PKO3: the scheduling tree under MAC 12 (L1..L5), channel 0xa00, LUT 0x280 | **kernel** builds it once with enough descriptor queues for everyone | the ioctl: the kernel's DQs (one per kernel CPU) and the application's DQs (one per dataplane core); the CN78XX has 1024 DQs |
| LMTDMA / IOBDMA scratch lines, XKPHYS user access | `ffn_xkphys.ko` as today, DP only | |
| the WQE software word(s) | shared layout, `octeon/kctl/ffn_dp_wqe_meta.h`, included by both | |
| the ITMH / RAW_DSA envelope encoder | shared header-only C, `octeon/native/ffn_itmh.h`, used by the kernel netdevs' `xmit`, the application's transmit and `ffn-portd`'s owners | |
| the port table (faceplate <-> BCM system port <-> logical port <-> netdev) | `ffn-portd` (the MP pushes it, `management/PORT-TABLE.md`); the kernel netdevs and the application both read the same published table | `/run/ffn-portd-table.json` today; a binary mirror through the ioctl for the hot path |

The forwarder's `cvmx3_hw_init()` today calls the SDK's
`cvmx_helper_initialize_packet_io_global()`, which would take over PKI and PKO
from the kernel. Under this contract that call goes: the application
initialises CVMX for CSR access (`cvmx_user_app_init`, `/proc/octeon_info`,
XKPHYS), opens `/dev/ffn-dp-packet`, maps the buffers, reads the resource
table, sets its group masks through the ioctl and uses the published DQs. The
`oct_hw_ops` seam (`ffn_dp_io_octeon.h`) is where that lands: a new backend
`OCT_HW_SHARED` beside `OCT_HW_CVMX3`, same work-get / send / free / frame-send
calls, no helper init.

### 3.2 SSO groups and what PKI decides at wire speed

| frames | PKI classification | QPG -> group | why |
|---|---|---|---|
| untagged and VLAN-tagged data from the front ports (styles 8 and 9 today) | as today, flow tag over the address/port window | application groups `A`, grptag low bits spread flows over the dataplane cores | the dataplane owns transit |
| FE100 returns (styles 10 and 11 by the fixed ITMH/CMH bytes) | as today | application groups `A`: misses, exceptions, type-2 punts and FLOWSTATS all belong to the session owner | the kernel never sees FE100 messages again |
| ARP (ethertype 0x0806), LLDP/LACP (destination `01:80:c2:00:00:0x`), the switch's own control | **new PCAM entries** (one per bank rule: the parser CAM conflict of 2026-10-20 is why entries never share a bank for one frame) -> a control style | kernel groups `K` | the kernel owns neighbours and link protocols; wire-speed steering keeps them off the dataplane cores and makes an ARP storm a kernel problem |
| IP to the box itself (management, VPN, DHCP, services) | not identifiable by PKI (no destination-address term in the PCAM) | application groups; the dataplane's local-address check punts them (3.4) | |

### 3.3 Buffers and work-queue entries

- Every buffer carries its aura in WQE word 0 (bits 48..59); **whoever frees a
  buffer frees it to the aura the WQE names**, validating the address against
  that aura's published range (the kernel does this today for its single aura;
  it becomes a lookup over the published pools). PKO frees on transmit per the
  descriptor's `df` bit, to the aura named in the SEND_HDR, exactly as the
  forwarder's `oct3_build_desc` already does.
- The kernel's `first_skip` reserves software words between the WQE and the
  link word. `ffn_dp_wqe_meta.h` defines word 6 (and 7 if needed): ingress
  logical port, VLAN unit, vsys, punt reason, flags (`envelope-stripped`,
  `from-kernel`), and the dataplane's session id for a punt the kernel may
  answer (ICMP generation names the session it answers for).
- A punted frame is **the original buffer**, not a copy: the application
  adjusts the WQE's data pointer and length to skip the 4-byte envelope (and the
  32-byte FE100 header for a return), writes the meta word, and submits the WQE
  to a kernel group with `ADDWQ`. The kernel's receive reads the meta word
  instead of re-parsing the envelope.
- Punts are bounded per reason per core (token bucket in the application) so a
  flood of unroutable packets cannot fill the kernel groups' admission queues;
  the kernel's receive counts per-reason drops when its own budget is exceeded.
- Nothing is ever freed twice: the application's disposition tracking
  (`enum oct_disp`, `bug_double_dispose`) stays, and a buffer handed to the
  kernel is `OCT_DISP_SENT` from the application's point of view.

### 3.4 Exceptions and local traffic reach the kernel's per-port netdevs

The kernel's receive on groups `K` sees two kinds of WQE: PKI-classified control
frames (3.2) and application punts (3.3). For both it maps the ingress logical
port from the meta word (or, for PKI-classified frames, from the OTMH source
system port in the envelope through the port table) to the per-port netdev
`pN` / `aeN.vlan` and delivers an ordinary Ethernet frame to the stack. From
there `ffn_ifmgmt_*`, the management stack, the VPN gateway, the DHCP server,
the routing daemon and conntrack for the kernel's own flows work exactly as on
any Linux router. Reasons the application punts: local delivery, no route, no
neighbour (the kernel resolves and the dataplane retries from `pending-neighbour`),
TTL expired, fragmentation needed, non-first fragment, unsupported protocol,
ALG session, policy says `kernel`.

### 3.5 The kernel's own transmit and its ITMH

A per-port netdev's `xmit` builds the 4-byte ITMH naming the port's BCM system
port plus the 8-byte RAW_DSA area (bytes 1..3 carry the aggregate owner's LAG
key for an aggregate member; byte 0 and bytes 20..23 of the envelope stay zero
as the driver enforces today) with `ffn_itmh.h`, from the published port table,
and issues the PKO command on **its CPU's own DQ**, under no shared lock. The
application builds the same envelope with the same header for its forwarded
packets. Transmit completions stay per DQ owner (the kernel's completion word
per buffer today; the application's `df=0` frees). One encoder and one table
means the two programs cannot disagree about where a port is.

### 3.6 Kernel inject into the dataplane

The reverse of a punt, for packets the kernel originates that must obey policy:
VPN inner packets, locally originated traffic to the LAN that a rule governs,
and whole sessions the dataplane marked `kernel` (ALG). The kernel allocates a
buffer from an application aura, copies the skb, writes meta word 6 with
`from-kernel` and the logical ingress (the tunnel or the netdev), and submits
to an application group. The dataplane treats it as an ingress packet on that
logical port. Rate-bounded the same way.

### 3.7 What must not diverge

- One resource table, published by the kernel, read by everyone; no SDK helper
  init in the application.
- One WQE meta layout (`ffn_dp_wqe_meta.h`), one envelope encoder
  (`ffn_itmh.h`), one port table.
- Free to the aura the WQE names; validate against published ranges; never
  dereference a WQE after handing it off.
- PCAM: one entry per bank per frame class; the driver keeps refusing to open
  when a foreign entry is valid.
- Group ownership is static for a boot; changing `K`/`A` is a DP restart.
- Both programs expose the same counters (`rx_per_group`, drops per reason,
  punts per reason, injects) in the driver's debugfs status and the
  application's shared-memory stats so one view explains a lost packet.

---

## 4. Inspection tiers

### 4.1 The tiers

| tier | what runs | where the bytes go | class |
|---|---|---|---|
| 0: forward | session hit, rewrite, PKO; or the FE100 | nothing scanned | line rate (1.5) |
| 1: App-ID | protocol parsers and a software DFA over the first payload packets of a flow, both directions, then a policy re-evaluation with the application known; eligible flows handed to the FE100 | <= 16 KiB per flow (the forwarder's `DP_ENGINE_FLOW_PKTS` x `DP_ENGINE_SCAN_MAX` bound) | 17 Gbit/s |
| 2: content | TCP reassembly, software Aho-Corasick with a prefilter over every cleartext byte, file-type and antivirus signatures, decompression; CA1 or an on-chip matcher when available | all payload of flows whose rule has an inspection profile | 9 Gbit/s |

A rule's profile selects the tier; a flow in tier 2 never offloads; a flow in
tier 1 offloads once identified and allowed; tier 0 offloads at creation.

### 4.2 The App-ID tier (17 Gbit/s class)

Identification first by structure, then by signature. A dozen protocol parsers
in C cover the bulk of Internet traffic: TLS ClientHello (SNI, ALPN, JA3/JA4
fingerprint), QUIC Initial (decrypted with the public version salt, SNI),
HTTP/1 request line and `Host`, HTTP/2 preface, DNS, SSH banner, SMTP/IMAP/POP
banners, FTP, NTP, SIP, RTP heuristics. What they do not settle goes to a
compressed DFA compiled on the MP from open signature content (4.4), run over
the same head-of-flow bytes with the scan position carried on the session per
direction (the content engine `ffn_dp_ctd` already does this for literals). The
result is an application id and a confidence; the rule is re-evaluated with it
(a rule may name applications; "unknown" is a legitimate match target); the
decision is written back to the session, and an allowed flow with no further
profile is admitted to the FE100 at that moment. Encrypted flows are identified
at the handshake and never scanned further unless TLS inspection exists.
Budget: 6.5 cores at the vendor's session rate (1.4); at today's 50..150
sessions/s it is noise.

### 4.3 The full-stream tier (9 Gbit/s class)

- **Reassembly** in order, per direction, from a bounded per-core segment pool;
  out-of-order segments are held up to a per-flow byte and segment budget and
  the flow is punted to the kernel path (or dropped, per profile) past it; no
  allocation on the packet path beyond the pool.
- **Prefilter** over 4-byte grams (shift-or or Bloom) at 4..6 cycles/byte,
  compiled with the automaton so a window can only reach the full matcher if it
  contains a gram some pattern contains.
- **Compact Aho-Corasick** (32..128 B per state, L2-resident) replaces the
  1 KiB-per-state table for production sets; the scan interface
  (`dp_aho_scan`: caller-held state, callback per match, zero allocation) stays,
  so the content engine and its tests do not change.
- **Structural detectors** (the DLP engine's digit runs and Luhn) stay beside
  it as today.
- **File-type detection** gates signatures by type; **antivirus** body
  signatures (hex patterns with wildcards) compile into the same automaton with
  wildcard continuation handled by a second-stage verifier.
- **Verdicts** `alert / block / reset` as the engine set defines; a block
  convicts the session so later packets drop without scanning.

Budget: 24 cores at 36 cycles/byte for 9 Gbit/s of cleartext, which the compact
automaton plus prefilter meets with margin and the current full-table matcher
does not.

### 4.4 Open content that can feed the tiers

| content | license | feeds |
|---|---|---|
| Zeek's dynamic protocol detection signatures (`dpd.sig` per protocol) | BSD | tier 1 DFA: regexes over initial payload per protocol, exactly the App-ID shape |
| Emerging Threats Open ruleset | BSD | tier 1 `app-layer-protocol` facts and tier 2 content patterns (Snort/Suricata `content:` and `pcre:` keywords; only literal and bounded-automaton parts are compiled, backtracking regex is refused as `ffn_dp_engine.h` already states) |
| Snort 3 community rules | GPLv2 | tier 2 content patterns (content, not code) |
| ClamAV signature databases (`main`/`daily`: `.ndb`/`.ldb` body signatures, `.hdb` hashes) | GPLv2 | tier 2 antivirus automaton and hash checks |
| JA3/JA4 fingerprint lists, IANA ports, the public QUIC/TLS/HTTP specifications | public | tier 1 parsers |
| URLhaus, OpenPhish community, oisd/hagezi DNS blocklists | CC0 / community | DNS and URL category checks in tier 1 |
| FFN's own signatures (the `dp.ctd.sig.*` config) | ours | both |

None of the vendor's `appsigs`, `threats/*`, `tdb_fpga_token_*` or MLAV content
is used; it is encrypted, licensed and off limits by rule. A compiler on the MP
(Python, as it is control-plane glue) turns the open content into the two
automata and the parser tables; the dataplane only ever loads compiled blobs.

### 4.5 Double-buffered content update

The vendor keeps every compiled artifact as `.0`/`.1` and flips. FFN's
forwarder already has the shape for policy: two 4 MiB banks and
`dp_activate_bank`. Content needs its own region (automata run to tens of
MiB): two banks sized by the compiled content, loaded into the inactive one
from the MP's blob (sha256 verified against the running configuration digest,
like the policy barrier), then activated by a per-core pointer swap between
packets. Sessions carry a bank generation with their scan state; a session whose
state belongs to the retiring bank restarts its scan at the root on its next
packet (the same reset the content engine does on truncation), and the retiring
bank is freed only when no session references it. Zero drops, zero stalls, and
a rollback is activating the previous bank.

### 4.6 The OCTEON ZIP engine

Compressed HTTP bodies (`Content-Encoding: gzip`/`deflate`) are opaque to a
matcher. The CN78XX ZIP unit inflates deflate streams in hardware with a
per-stream context (the SDK's `cvmx-zip.h`; the vendor used it:
`pan_cvmx_zip_reset_offloader`). The HTTP parser puts a session into inflate
mode; payload chunks are submitted asynchronously, the inflated output lands in
buffers from a dedicated pool and is scanned, with a hard bound on inflation
ratio and inflated bytes per flow (decompression bombs are a rule verdict, not
a crash). Brotli and zstd are not deflate and are not inflated in phase 6.

### 4.7 Hardware matchers

- **CA1/CE40.** The socket is one FPGA whose personality the bitstream picks
  (`a101` after a reset, `a00d` when `ce40.bin` is programmed and Linux boots
  without a reset); the register file, the four CIP units at 0x1000 stride, the
  table-load word format and the DDR arena enables are measured; the DDR-ready
  gate has never been seen set on this unit, the DP's Interlaken to it failed on
  the vendor's own boot, and its config word and request/response formats over
  that link are unknown. It stays an **optional accelerator behind the same
  pattern-set compiler** (one set, two targets) and off every critical path.
- **The OCTEON's own regex engine.** The part suffix `AAP` marks Cavium's
  accelerator-complete variant, which in the OCTEON III family includes the
  ZIP unit and a hardware finite-automata engine; the vendor's symbols
  (`pan_ctd_clear_wqe_2_regex_offload`, `*_sw_offload` as the fallbacks)
  suggest the DP library used a work-queue-driven regex offload beside its
  software paths. Whether `CN7885-AAP` populates it is one read-only probe
  (section 7). If it does, tier 1's DFA and much of tier 2 move off the cores
  for the cost of a WQE per scan; the design above does not depend on it.

---

## 5. The phased plan

Every phase lands on the production box in a maintenance window, has a rollback
that is a file swap or a service restart, and has a gate measured on the box.
Measurement harness, common to all phases: two 10G hosts on SFP+ faceplate ports
(or the QSFP28 pair 23/24 at 40/100G) in a lab zone pair, iperf3 multi-stream
for throughput, a packet generator (TRex or `pktgen`) for IMIX and
sessions-per-second, the DP's own counters (trunk debugfs status, the dataplane's
shared-memory stats, FE100 `nif rx / flow hits / egr forward / nif tx`) and
per-CPU busy from `/proc/stat` sampled at 1 s. Production traffic is never the
load generator; the WAN path is the regression check (download and upload
unchanged, zero rejected returns, zero in-box drops).

### Phase 1: the FE100 offload program as it stands (under way)

- **Goal**: long-lived flows carried in the chip from the kernel path, with
  admission by the CP loop, so the kernel's per-packet cost stops being the
  box's ceiling for bulk traffic.
- **Deployable unit**: PR #102 to `admit` mode after its `settled()` invariant,
  `bindings()`/`paths()` wired to `AttachmentOwner` and `PathOwner` (the
  commissioning `production_attach_cp.py` recipe productised in the control
  service), an `activate` control op under the MP commit barrier; steering of
  BCM 16 (WAN) and the ae1 members 32/33 into port 3 with the return path 20 ->
  24 (the 2026-10-20 eight-hour window passed on 16; the LAG members need
  either hardware egress selection or a fixed member per session as run 8 did);
  the DP side of an admitted session: `ffn_ctlease` lease at `open`, release at
  `close`, type-2 punts into the kernel path (`ffn_fe100_punt.c`, already
  decoding them), FLOWSTATS consumed by one durable owner (`NativeCounterStream`
  + `AccountedSession` instantiated in production, which nothing does yet).
- **Gate**: during a one-hour soak with the LAN's normal traffic, >= 90 % of
  transit bytes forwarded by the FE100 (`egr_condor_fwd` bytes vs trunk rx
  bytes), download and upload unchanged at the 1G client, zero rejected returns,
  FIN/RST hand-back closing sessions within one RTT, the kernel path's CPU
  (`ffn-packet-rx*` plus the lanes) down by half.
- **Touches**: `fe100/ffn_fe100_admission_loop.py`, `ffn_fe100_controld.py`,
  `ffn_fe100_policy_control.py`, `ffn_fe100_offload_policy.py`,
  `ffn_fe100_accounting.py`, `fe100/commissioning/*` (productised),
  `octeon/native/ffn_portd.c` (lease and counter owner), `bcm/` steering
  recipes, core `dataplanes/ctlease`.
- **Retires**: the lab diverter `steer_dp.py` and the per-run `ct_liberal.py`.
- **Rollback**: `/etc/ffn/fe100-admission.json` back to `observe`; force-forward
  16/32/33 -> 24.

### Phase 2: the kernel trunk driver as the shared owner, with per-port netdevs

- **Goal**: retire the TAP path. Linux forwards natively on kernel netdevs;
  the driver owns PKI/SSO/PKO under the contract of section 3, with the
  application groups, DQs, buffer region, meta word and ioctl defined even
  though no application uses them yet.
- **Deployable unit**: the new driver module (load-once per boot, so a DP
  restart), `ffn-portd` attaching kernel netdevs instead of TAPs, core
  `ffn_linux_network.py` configuring a netdev it did not create, the FE100
  return decoder moved to where the returns now arrive (kernel groups in this
  phase), `ffn_itmh.h` and `ffn_dp_wqe_meta.h`.
- **Gate**: 1518-byte bidirectional iperf3 through the kernel path on the lab
  pair >= 8 Gbit/s aggregate on 8 cores (per-core DQs proven: no `trunk_tx_lock`
  saturation), IMIX >= 2 Gbit/s, p95 forwarding latency <= 1 ms; cycles per
  forwarded packet <= 25 k (per-CPU busy at the measured pps); WAN regression
  unchanged; FE100 returns decoded with zero rejects at the phase-1 rate.
- **Touches**: `octeon/kctl/ffn_dp_trunk.h` (split into `ffn_dp_trunk.c`,
  `ffn_dp_netdev.c`, `ffn_dp_resources.c`), `ffn_dp_dma.h`,
  `ffn_dp_packet_init.c`; `octeon/native/ffn_portd.c`, `ffn_packet.c` (physical
  RX/TX workers and lanes retired; the control receivers for LACP/LLDP stay);
  `octeon/native/ffn_fe100_punt.c` / `ffn_fe100_stats.c` (callers change); core
  `opt/ffn_linux_network.py`, `ffn_packet_cpus.py`, `ffn_tap_steering.py`.
- **Retires**: TAPs, `multi_queue` remakes, `/etc/ffn/packet-lanes.json`,
  `PACKET_FANOUT` steering, txqueuelen tuning, the CPU reservation allocator for
  lanes, `libffn-packet.so`'s forwarding role.
- **Rollback**: the previous `.ko` and `ffn-portd` build in the NFS root, DP
  restart (the 2026-10-20/21 driver swaps are the template).

### Phase 3: the userspace dataplane takes transit, inspection off

- **Goal**: data traffic forwarded by `dpfwd` with the session table, policy
  and NAT in C; the kernel keeps section 2.4's list; admission to the FE100
  decided at first packet on the DP and installed through the CP path; the
  session feed produced in C.
- **Deployable unit**: `ffn-dp-octeon` running on the application cores under
  the shared owner (`OCT_HW_SHARED` backend), the policy and NAT compiler output
  shipped beside the nft rendering (both exist for a while: the kernel path is
  the rollback), `ffn-dp-export` replacing `ffn_session_stream_dp.py`, the
  netlink mirror for FIB, neighbours, addresses and links with staged commit,
  the punt and inject paths live. Switched per port: a port table row says
  `path: kernel|dataplane`, so the WAN can stay on the kernel while the lab pair
  proves the dataplane, then one port at a time.
- **Gate**: lab pair at 1518 B >= 30 Gbit/s aggregate, IMIX >= 10 Gbit/s,
  >= 100 k new sessions/s with a 200-rule policy and NAT (generator-measured,
  session table and feed counters agreeing), p99 forwarding latency < 200 us,
  zero buffer leaks over an hour (aura counts return to baseline), both
  steering models of 2.2 measured and one chosen; WAN regression unchanged
  after its row flips.
- **Touches**: `octeon/dpfwd/` (new `ffn_dp_session.c/.h`, `ffn_dp_tcp.c`,
  `ffn_dp_policy.c` replacing the linear `dp_classify`, `ffn_dp_nat.c`,
  `ffn_dp_netlink.c`, `ffn_dp_punt.c`, `ffn_dp_io_shared.c`, `ffn_dp_export.c`,
  `ffn_dp_abi.h` v3 with the session stats and event rings; `dp_arp.c` disabled
  on this platform), `octeon/kctl` (the application ioctl), core
  `opt/ffn_policy_plan.py` and the fastpath compiler (zone-aware `policy.bin`,
  `nat.bin`), `opt/ffn_session_events.py` (its journal schema becomes the
  exporter's output contract; the collector itself is no longer on the transit
  path), `management/session_relay.py` (projection paging),
  `fe100/ffn_fe100_controld.py` (the DP's admission request ring as a second
  intake beside `Observations`).
- **Retires** for ports on the dataplane: conntrack as the session table, the
  `ffn_security`/`ffn_nat` forward chains' role (the kernel keeps them for its
  own flows), `ffn_session_stream_dp.py`, `ffn_l3_offload.py`'s planning (the
  dataplane plans from its FIB), the `should_offload` thresholds as the
  decision (kept as the audit).
- **Rollback**: flip the port's row back to `kernel`; stop `ffn-dp-octeon`.

### Phase 4: fast FE100 install and short-flow offload

- **Goal**: sessions reach the chip within microseconds of the decision, from
  the DP, so the FE100 carries short flows too and the DP's cost per session is
  the creation cost only.
- **Deployable unit**: the in-band install path (request and acknowledgement
  formats recovered read-only, proven on the port-13 loop, then enabled per
  port), flow-id allocation per core from ranges the CP hands out, FLOWSTATS
  aging and type-2 hand-back entirely in the dataplane, `SESS_AGEOUT` handling
  if the chip emits it; the CP path kept as fallback and for the audit.
- **Gate**: >= 100 k offloaded sessions/s sustained for ten minutes with the
  table and the chip's hit counters agreeing; >= 95 % of bytes in hardware on
  a mixed-flow generator run; install latency p99 < 100 us measured by the
  dataplane; with offload on, the 40 Gbit/s one-way forwarding test leaves the
  dataplane cores < 20 % busy.
- **Touches**: `octeon/dpfwd/ffn_dp_offload.c` (new), `fe100/` message codecs
  (C, from `ffn_fe100_stats.c`/`ffn_fe100_punt.c` outward),
  `fe100/ffn_fe100_flow_namespace.py` (range leasing), the CP control service
  (fallback intake, audit).
- **Retires**: the CP as the per-session installer; the Python counter stream
  and aging modules (`ffn_fe100_flowstats.py`, `ffn_fe100_aging.py`,
  `ffn_fe100_accounting.py`) once the C owner has run a month.
- **Rollback**: disable the in-band path per port; the CP path resumes.

### Phase 5: App-ID (the 17 Gbit/s class)

- **Deployable unit**: the protocol parsers and the DFA engine in the
  dataplane, the content compiler on the MP, rules by application in the
  policy compiler and the UI, offload after identification, the content bank
  region and flip.
- **Gate**: 17 Gbit/s aggregate with App-ID on every flow (replay of a labeled
  mixed capture at speed on the lab pair plus iperf3 fill), identification
  >= 95 % on the labeled corpus with zero misidentified blocks, >= 50 k new
  sessions/s with identification, content flip under load with zero drops.
- **Touches**: `octeon/dpfwd/ffn_dp_appid.c`, `ffn_dp_dfa.c`,
  `ffn_dp_content_bank.c`; `management/content_compile.py` (new); core policy
  schema and WebUI for application objects.
- **Retires**: nothing; adds a tier.

### Phase 6: full-stream inspection (the 9 Gbit/s class)

- **Deployable unit**: reassembly, the compact automaton and prefilter, ClamAV
  and ET/Snort content compiled into it, file-type detection, ZIP inflate,
  profiles in the policy; CA1 or the on-chip engine behind the same compiler
  if 4.7's checks come back positive.
- **Gate**: 9 Gbit/s aggregate of cleartext replay with a profile on every
  flow, zero false positives on a benign corpus, the published detection rate
  on a malicious corpus, p99 latency < 1 ms, content updates with zero drops,
  decompression-bomb corpus handled by verdict.
- **Touches**: `octeon/dpfwd/ffn_dp_reasm.c`, `ffn_dp_aho.c` (compact
  encoding behind the same API), `ffn_dp_prefilter.c`, `ffn_dp_ftype.c`,
  `ffn_dp_zip.c`; `octeon/nac/` if CA1 is pursued.
- **Retires**: the 1 KiB-per-state matcher for production sets.

### Phase 7: the 40 Gbit/s circuit

- **Deployable unit**: a QSFP28 WAN (ports 21..24 at 40G or 100G), the
  attachment and steering for it, the FE100 as the forwarding path with the DP
  as admission and inspection in front, and, if ports 25/26 exist, the DP link
  LAG.
- **Gate**: a line-rate generator on the circuit side: 40 Gbit/s each way
  forwarded with inspection off, 17 Gbit/s each way with App-ID, 9 Gbit/s with
  full inspection, at the session rates above.

Ordering and what is not thrown away. Phase 1 is the FE100 program as it is,
and every piece of it survives: the attachment, path, flow-id and lifecycle
owners become the CP's table operator for phases 3 and 4, the admission loop
becomes the audit, the return decoders and counter codecs are reused in C.
Phase 2 is the sibling proposal and is a prerequisite for 3 (the application
needs an owner to get resources from) but useful alone. The forwarder's I/O
backend, FIB, ECMP, vsys plan, flow cache, engine set, content engine and tests
all carry forward; only the SDK helper init and ARP are set aside on this
platform.

---

## 6. Is it worth it

**What the kernel netdev path alone delivers (phases 1 and 2, no userspace
dataplane).** Forwarding in the 10 Gbit/s class at large packets and 3..9
Gbit/s at IMIX, with the FE100 carrying long flows after a Python admission
that sees a flow seconds after it starts and installs through the CP at a few
hundred sessions per second at best. For today's 1 Gbit/s circuit that is more
than enough and it is a large step up in headroom and latency. For a 10 Gbit/s
circuit it works for bulk-heavy traffic and degrades as the flow mix gets
shorter. It cannot reach 40 Gbit/s one way through the DP at IMIX, cannot offer
169 k sessions/s (conntrack creates them at perhaps 20..50 k/s on this core
before the Python feed falls behind, as it already does at 25 events/s today),
and it cannot inspect a byte: any inspection on this path is a copy to
userspace, which is the TAP path again. No amount of kernel work closes that.

**What the userspace dataplane costs.** `dpfwd` exists at about 14 k lines
including tests, with the parts that are hard to get right on this silicon
(FPA3 auras, WQE-inside-buffer ownership, PKO3 descriptors, the CVMX userspace
runtime, per-core FIB with ECMP, a bounded flow cache, an engine set with a
streaming matcher) written, compile-checked against the SDK and tested on both
byte orders. What is missing, in new C: the session table with TCP state and
both tuples (about 2 k lines), the compiled policy classifier (1.5 k), NAT
(1.5 k), the netlink mirror with staged FIB commit (1 k), the shared-owner
backend plus punt and inject (1 k user, 1.5 k kernel), the session exporter
(1 k), the FE100 install, FLOWSTATS and hand-back in C (2 k) and their tests:
roughly 12 k lines for phases 3 and 4. App-ID and content inspection are
another 10..15 k (parsers 3..5 k, DFA and compact AC with compiler 3 k,
reassembly 2 k, prefilter and file types 1 k, ZIP 1 k) plus a Python content
compiler. At the pace this repository has shown (several hardware-proven PRs a
week), phases 3 and 4 are a couple of months of focused work and phases 5 and 6
another couple, each with maintenance windows the box has already learned to
take.

**The verdict.** If the goal were only to forward a 10 Gbit/s circuit without
inspection, phases 1 and 2 would be the honest stopping point. The owner's
targets are 40 Gbit/s forwarded and 17 Gbit/s inspected. The first needs a
microsecond admission engine in front of the FE100 that can see every first
packet, and the second needs payload inspection on the DP cores; both are the
userspace dataplane and nothing else on this board. The work is substantial but
the risk is concentrated in two places, both testable in isolation before any
production port moves: the in-band FE100 install format (phase 4, with the CP
path as a working fallback) and the steering model of 2.2 (phase 3, A/B on the
lab pair). Everything else is engineering on foundations that already run on
the chip.

---

## 7. Read-only checks to run before writing code

1. **Are BCM ports 25 and 26 wired to the DP?** Read the DP's `GSER(n)_CFG` /
   QLM configuration and `BGX(0,1)_CMR(0)_CONFIG` for interfaces 0 and 1, and
   the switch's `xl25`/`xl26` link state. Decides whether the DP cap is 40 or
   120 Gbit/s.
2. **Does CN7885-AAP have the regex engine and the ZIP unit?** The SDK's
   feature test (`octeon_has_feature(OCTEON_FEATURE_HFA)`, the ZIP feature) and
   one CSR read of each block's identity register from the DP. Decides whether
   tier 1 and tier 2 get a hardware option without the CA1 program.
3. **The in-band FE100 session install format.** Field layouts of the
   Octeon-to-FE100 control messages in the vendor's `pcs/packets/fe100.py` and
   the DP library's DWARF (interface facts only), the `hif`/`cfp` block
   registers that gate it, and whether `SESS_AGEOUT` is emitted. Decides phase
   4's shape.
4. **The PKO3 descriptor-queue and PKI style/QPG budget** the shared owner can
   publish: SDK resource tables for the CN78XX against what the kernel driver
   already allocates. Decides `K`/`A` group counts and DQ assignment.
5. **Kernel forwarding cost on this core**, measured rather than estimated:
   with the WAN at a known pps, per-CPU busy of the kernel path after phase 2's
   first lab run. Replaces the 15..25 k estimate.
6. **Flow memory capacity** of the FE100 as initialised ("capacity 4"): the
   FLU/FDT configuration registers against the DDR tables documented in
   `DDR-EXTERNAL-TABLES-20260914.md`. Decides how the 4-million figure is
   treated.

---

## Appendix: derivations and sources

- Cycles: 40 cores x 1.6 GHz = 64.0 Gcycles/s; 32 cores 51.2; 16 cores 25.6.
  Packets/s = R / (8 (F + 20)). IMIX 7:4:1 of 64/594/1518 B = 362 B mean.
  Payload fraction 1460/1538 = 0.949 at 1518 B.
- TAP path 64 k cycles/packet: 2026-10-20 measurement, three cores at 70..81 %
  busy at about 75 kpps per direction (`ffn-line-rate-program-20261020`,
  `ffn-40g-architecture-tiers`). Driver receive about 15 k: 70..82 % of one core
  at 570..660 Mbit/s with one receive thread.
- Kernel netdev estimate: Linux conntrack + nft + NAT forwarding at 3..6 k
  cycles on an out-of-order x86 core, scaled 2.5..3x for an in-order dual-issue
  cnMIPS III, plus receive without the socket copy (about 8 k) and PKO submit
  and completion (about 2 k).
- Userspace hit path 2..4 k: sum of the per-stage costs in 1.3; DRAM miss about
  100 ns = 160 cycles; L2 hit about 40 cycles (CN78XX family data; not measured
  on this unit).
- Matcher costs: the forwarder's own Aho-Corasick (`ffn_dp_aho.h`: 1 KiB per
  state, one indexed load per byte) as the full-table reference; compact AC
  and DFA figures are typical for cache-resident automata on in-order cores.
- FE100 formats and limits: `fe100/PHYSICAL-RETURN.md`, `COUNTERS.md`,
  `FLOW-ID-OWNERSHIP.md`, `SESSION-LIFECYCLE.md`, `NAT-ACTIONS.md`,
  `fe100/commissioning/README.md` (run 8 evidence), `ffn_fe100_sessions.py`,
  `ffn_fe100_session_adapter.py`, `ffn_fe100_resource_tables.py`.
- Admission loop thresholds and timers: `fe100/ADMISSION-LOOP.md` and
  `ffn_fe100_offload_policy.py` on `codex/fe100-admission-policy` (PR #102).
- Session feed protocol: `management/session_stream.py`, `fe100/SESSION-STREAM.md`,
  `octeon/debian/ffn_session_stream_dp.py`, `management/session_relay.py`,
  `management/session_inventory.py`.
- Kernel path today: `FFN-NGFW/opt/ffn_security_runtime.py` (gate, drift,
  replay, collector), `ffn_security_nft.py` (the rendered tables and what the
  renderer refuses), `ffn_nat_runtime.py` and `ffn_nat_policy.py`,
  `ffn_session_events.py` and `ffn_session_feed.py` (conntrack netlink and the
  row schema), `ffn_interface_management.py`, `ffn_interface_services.py`,
  `ffn_service_routes.py`, `ffn_protect_gateway.py`, `ffn_static_routes.py`,
  `ffn_l3_offload.py`, `dataplanes/ctlease/`; live ruleset read in `ffn-data`
  on 2026-10-21.
- Kernel trunk driver: `octeon/kctl/ffn_dp_trunk.h`, `ffn_dp_packet_init.c`,
  `octeon/debian/PACKET-RUNTIME-20260914.md`; live status from
  `/sys/kernel/debug/ffn_dp_packet_init/status` on 2026-10-21.
- Forwarder: `octeon/dpfwd/` (`ffn_dp_oct.h`, `ffn_dp_io_octeon.h`,
  `ffn_dp_io_octeon3.c`, `ffn_dp_l3.h`, `ffn_dp_engine.h`, `ffn_dp_ctd.h`,
  `ffn_dp_aho.h`), `octeon/DP-FORWARDER.md`, `octeon/PACKET-PATH.md`.
- CA1/CE40: `octeon/ca1/README.md`, `BAR0-MAP.md`, `AHO-INTERFACE.md`,
  `ACCESS-PROTOCOL.md` (on `fix/ci-vif-forwarding`), `octeon/nac/`.
- Faceplate and switch ports: `FACEPLATE-MAP.md`, `octeon/BCM88375-PORTS.md`.
- Vendor figures for this model (about 17..18 Gbit/s App-ID, about 9 Gbit/s
  threat, 169 k new sessions/s, about 4 million sessions) are the published
  sheet values the owner quoted on 2026-10-21; they are targets, not
  measurements of this unit.
