# FE100 hardware NAT packet qualification

The isolated front5/front13 loop now supports address and port translation
tests for IPv4 UDP and TCP. These tests exercise actual FE100 session entries,
physical front-port egress and return capture. Production traffic continues
through the OCTEON kernel provider; successful lab packets do not authorize
production session admission.

## Physical result, 2026-09-22

All eight matrix cases passed on the PA-5220 isolated front5/front13 loop:

| Protocol | Translation | Forward | Reverse |
| --- | --- | --- | --- |
| UDP | Address | 4/4 exact frames | 4/4 exact frames |
| UDP | Address and port | 4/4 exact frames | 4/4 exact frames |
| TCP | Address | 4/4 exact frames | 4/4 exact frames |
| TCP | Address and port | 4/4 exact frames | 4/4 exact frames |

Every case passed baseline, miss, hit, drop, removal and cleanup checks with
zero reported socket drops. The audited set shares one CP boot, BCM lifetime
and production aggregate owner. After the BCM lock-yield fix, both aggregate
members remained distributing throughout this set. One TCP test attempt was
refused before hardware changes because the FE100 table lock was busy; its
failed report is excluded, and the subsequent complete run is included.

[Recorded results](NAT-PACKET-RESULTS.json) contain the case summary and source
report hashes. Full packet reports and their combined audit remain on MP at
`/var/log/ffn-fe100-nat-qualification.json`. These are sequential rewrite tests,
not TCP connection or production NAT qualification. The focused MIPS64 Python
suite passed 105 tests.

## Queue-map correction

The original-address QMAP fixture produced four session hits, four egress
exception17 events and unchanged packets at diagnostic capture. QMAP must match
the session's **translated** source and destination addresses. Updating that
key produced physical rewritten packets and QMAP hits. Forward and reverse
address translation both passed exact packet checks.

QMAP's destination queue is obtained from the current BCM queue inventory via
`BCM_GPORT_UCAST_QUEUE_GROUP_QID_GET`. Queue numbers from an earlier switch boot
are not valid configuration. Hardware port mapping remains part of the board
module; test addresses and VLANs remain confined to isolated fixtures.

## Control and capture reliability

Releasing the shared BCM lock between calls was insufficient: immediate lab
reacquisition could repeatedly beat the production heartbeat's retry loop.
The lab now yields for 100ms after releasing that lock. A unit test verifies
that the production lock is available during the yield. Active aggregate
owner identity is checked between phases; an owner change aborts the test.

AF_PACKET capture uses an enlarged per-socket receive buffer, with no global
network sysctl change. Both MP and DP report socket-drop counts. Any capture
loss prevents qualification. Packet submission alone is never accepted as
evidence of physical forwarding or successful translation.

## Qualification procedure

Use only the confirmed isolated front5/front13 cable loop. Establish the
current FE100 commissioning prerequisites described in
[retained-state commissioning](WARM-COMMISSIONING.md) immediately before each
bounded run. The harness owns and restores its temporary FE100 entries, BCM
rules and redirects, and MP capture settings.

On MP, each combination of protocol, translation and direction is separate:

```sh
python3 /usr/local/sbin/validate_front_sessions.py \
  --cross --vlan-return --nat address --protocol udp --count 4
```

Use `--nat port` for address-plus-port translation, `--protocol tcp` for TCP
segments, and `--front5` for reverse translation. Each test requires baseline,
miss, hit, drop and removal phases. Successful hit packets must match the full
expected Ethernet frame, translated tuple, VLAN, decremented TTL, IPv4 checksum
and transport checksum. Miss/hit/drop counters and cleanup must also agree.

`validate_nat_results.py REPORT.json ...` independently audits completed MP
reports. It recomputes expected packet bytes, checks physical-return metadata,
rejects failed cleanup/capture loss, and refuses to combine different CP/BCM
or production-owner lifetimes. Its matrix has eight cases: TCP/UDP, address/port
translation, and both directions. Missing cases remain explicit. The result
always reports `production_admission: false`.

## Limits

TCP fixtures are ACK+PSH segments for rewrite/checksum testing; they do not
establish TCP connections or validate retransmission/teardown tracking. Runs
are directional and sequential. Production integration still requires paired
session admission, ordered invalidation on policy/interface changes, aggregate
and VLAN transit attachment, aging/counter ownership, and restart recovery.
UDP without a checksum, fragmentation, IPv6 NAT and sustained load are outside
this packet matrix. The production adapter's qualification gates remain intact.
