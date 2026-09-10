# Inline analysis commissioning

The Debian DP fabric now calls the existing C engine registry and DLP literal
scanner before delivering each selected ingress frame to its TAP. This applies
before either Linux bridge forwarding or IP routing. It does not inspect the
return SSH stream a second time. Physical attachment remains ports 1, 3, 5, 13.

`ffn_inline_adapter.c` parses Ethernet (up to two VLAN tags), unfragmented IPv4
and IPv6 without extension headers, and TCP/UDP header lengths. Only the L4
payload enters `dp_engine_scan`; Ethernet padding does not. Allocation occurs
when loading a policy, not per packet. The existing engine limits scanning to
2,048 bytes; the commissioning fabric is limited to MTU 1500.

## MP runtime control

`ffn-inspection status` reports the saved config and DP runtime revision, mode,
selected ingress ports, counters and reload errors. `ffn-inspection set` accepts
one printable ASCII literal of 1–63 characters, mode `off`, `alert` or `block`,
and a list of attached ingress port numbers. Fetch the current revision before
setting policy, for example:

```sh
ffn-inspection status
ffn-inspection set <<'JSON'
{"revision":0,"mode":"alert","ports":[1],"literal":"FFN_TEST_DENY"}
JSON
```

Use the actual current revision, not necessarily zero. `set` atomically saves
the next revision to `/etc/ffn/inspection.json` on DP. The live relay polls at
approximately one-second intervals, including when idle. Verify `running`, a
matching runtime revision and a null `reload_error` before assuming activation.
Changing policy does not restart the relay or change network configuration.

The MP wrapper uses the existing pinned MP→CP→DP SSH path. No listener is added.
Verdict counters accumulate across policy changes within a relay process; they
reset when it restarts. The policy file persists across restart. Without a file,
inspection defaults to off. Invalid reloads retain the last valid active policy
and expose the error. An unavailable C library rejects enabled configurations
at the controller. A relay starting with an unloadable saved policy reports a
reload error and remains off; this is not fail-closed IPS operation.

`alert` records a match and passes the frame. `block` drops a matching frame
before TAP injection. Counters include per-port verdicts and bypasses. Telemetry
contains counts and policy revision, not captured packet contents.

## Limits

This is the first packet-inspection integration, not a complete IPS/DLP product.
Fragments, IPv6 extension headers, non-TCP/UDP and unsupported encapsulations
pass with `unsupported_pass` counters. Invalid lengths pass with
`malformed_pass` counters for the downstream stack to handle. There is no TCP
stream reassembly, cross-packet matching, TLS decryption, session verdict cache,
application identification, reset injection, multi-rule policy API or hardware
offload. A split or encrypted literal will not be detected. No security claim
should be based on the absence of a match. Throughput remains bounded by the
software MP/SSH/DP commissioning fabric.

The C adapter reuses FFN's engine and scanner sources; it does not embed owner
firmware or an SDK library. The broader C registry supports other scanners, but
this first live management surface deliberately exposes only literal matching.

## Build and test

```sh
make -C octeon/dpfwd inline-test
make -C octeon/dpfwd libffn-inline.mips64.so
```

The cross-build requires `mips64-linux-gnuabi64-gcc`; on the build VM it is in
`/mnt/clones/debian-mips64/sid-host`. Run the build there using `chroot` with the
source staged inside it. Install the resulting shared object on DP as
`/usr/local/lib/libffn-inline.so`, alongside `ffn_inspection.py` and the updated
`ffn_fabric.py` in `/usr/local/sbin`. Install `ffn-inspection-mp` on MP as
`/usr/local/sbin/ffn-inspection`. Attaching a newly changed fabric implementation
requires one relay restart; subsequent policy updates are live. Preserve the
prior relay script as a rollback copy. No BCM/FE100 restart is required.

`test_inline_adapter.py` covers UDP/TCP, IPv4/IPv6, tags, padding, truncation,
bad lengths, unsupported fragment/extension paths, constructor validation,
10,000 deterministic random inputs, policy validation, per-port scope and
retention of the last valid policy after a malformed reload. The existing
`ffn_dp_engine_test.c` exercises the underlying scanners independently.

On MP, `test-fabric-matrix.py --inspection` runs the physical L2/L3 matrix plus
synthetic alert/block/clean/scope cases. It requires cables 1↔3 and 5↔13 and no
concurrent configuration writers. It restores the saved port settings and
inspection policy. Ports 3 and 13 are temporarily disabled as TAP endpoints so
captured test returns cannot recirculate through the cable loops.

## PA-5220 validation, 2026-09-09

The underlying engine tests passed on both VM x86 and the physical MIPS64-BE DP.
All eight adapter/policy tests passed on both architectures, including the
10,000-input bounds test. The native Makefile `inline-test` target also passed.

The physical matrix passed with the hook installed. Across L2, routed IPv4 and
routed IPv6, 180 synthetic matching packets were blocked; all 540 packets in
alert, clean-control and excluded-ingress-port cases forwarded intact. Per-test
alert and block counter deltas were checked at 60 matches each. The repeated
baseline also passed 1,200 intended forwards and 660 intended isolation/hop
drops. These are paced functional tests, not a throughput measurement.

Evidence: `INLINE-MATRIX-VALIDATION-20260909.jsonl`; the separate baseline run
is `FABRIC-MATRIX-VALIDATION-20260909.jsonl`. Network settings were restored to
their original values. Inspection is left off with the hook available for
runtime configuration. The fabric and thermal services remain active.

Installed MIPS64 library SHA-256:
`a0e79b2b1e60b752bcf8a0ea52d8eb044fb1f5b7652ea7e567b5fe3706d83ab1`.
