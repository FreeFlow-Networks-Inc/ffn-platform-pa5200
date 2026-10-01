# Independent FE100 withdrawal supervisor

Production FE100 admission remains disabled. The CP supervisor and isolated
physical recovery adapter now cover an owner that exits, stops making progress,
or leaves native helper processes behind. This closes a recovery gap; it does
not qualify a production policy provider or line-rate performance.

## Ownership and ordering

`ffn_fe100_guard.py` starts a trusted owner in a dedicated cgroup v2 leaf.
The child joins before executing owner code. A private inherited Unix
sequenced-packet socket carries a nonce, increasing sequence and local monotonic
timestamp. The owner pulses after dependency checks or bounded progress. It must
not run an unconditional heartbeat thread that could conceal a stalled owner.

The supervisor uses a pidfd and an independent deadline. Exit, silence,
malformed messages, stale timestamps and lost sequence continuity all end the
lease. It kills the bootstrap and entire cgroup, including helpers that called
`setsid()`, and verifies the group is empty before recovery acquires table locks.
The durable report stores the cgroup identity before launch. On restart, the
supervisor kills any orphaned group before recovering or starting another owner.
The embedding service must restart the supervisor after its own failure.

Recovery must acknowledge these steps in order:

1. Withdraw ingress admission to the affected FE100 path.
2. Remove the exact owned flows and verify their absence.
3. Restore dependent next-hop, QMAP, LIF and BCM resources.

`Withdrawal` enforces callback ordering and requires literal `True` responses.
Callbacks must have bounded native operations and verify hardware readback.
Any failed recovery leaves the report blocked and never starts another owner.
The generic supervisor itself never enables admission.

## Isolated physical adapter

`ffn_fe100_guard_lab.py` integrates the supervisor with `Lab` and the existing
native FE100/BCM adapters. The MP test harness must first verify that its selected
front ports are unconfigured in both candidate and running configuration, own
the isolated link, and restore port settings independently. The CP controller
does not enable links. Port selection comes from the existing lab profile; no
deployed firewall configuration is compiled into the controller.

The controller accepts `prepare`, `install`, `drop`, `remove`, `snapshot` and
`finish` JSON lines. Fault commands are rejected unless `--fault-injection` was
explicitly supplied. Its journal must be named `guarded-physical-*.json` inside
the FE100 state directory. Run it only from an isolated commissioning harness:

```text
ffn_fe100_guard_lab.py --journal /var/lib/ffn/fe100/guarded-physical-<run>.json
```

The supervisor has a 45-second startup allowance and a 25-second progress lease
for bounded native commissioning calls. This diagnostic timing is not a
production convergence guarantee. Native table workers retain their own
watchdogs. The CP requires writable cgroup v2 with `cgroup.kill` and pidfd support;
there is no weaker process-group fallback.

`ffn_fe100_lab_guard.py` is the sole physical cleanup owner. The child journals
SDK allocation intent before mutation and never deletes BCM objects. Recovery
checks the CP boot, BCM lifetime, pinned native owner, complete lab profile and
hashes of initialization journals before touching FE100 tables. Ingress is
returned to the baseline DP path before either NAT direction is deleted.
Unresolved flow deletion prevents restoration of resources used by that flow.

BCM allocation or deletion with a missing acknowledgement is deliberately
uncertain. Recovery never guesses IDs or retries a potentially reused ID.
Confirmed group absence is required before recording successful deletion.
An SDK operation interrupted in flight is not qualified by an owner crash
after a completed transaction. Punt-path experiments also need their own
recovery adapter and are rejected by this controller.

## Qualification

Both host and actual OCTEON CP process tests cover exit, `SIGSTOP`, silence,
malformed/replayed heartbeat, detached helper cleanup and supervisor restart
with an orphaned owner. Failure of the recovery callback remains blocked.

On the isolated front 23/24 fiber loop, paired UDP port NAT passed physical
owner-crash and frozen-owner tests. The 64-frame frozen-owner test rewrote all
64 frames before the fault. After lease expiry and native cleanup, all 64
post-recovery frames returned unchanged over the baseline path, with no stale
NAT rewrites. Both NAT directions and dependent resources were removed/restored,
the BCM rule was deleted and its group read back absent, the front ports were
restored, and running configuration was unchanged. Production services were
not restarted. This is functional recovery evidence, not a throughput result.
The packaged controller repeated the 64-frame frozen-owner test successfully.
See [the compact physical results](SUPERVISED-WITHDRAWAL-EVIDENCE.json).

Tests are named explicitly in CI:

```sh
cd fe100
python3 -m unittest test_guard test_lab_guard test_guard_lab test_packet_sessions
sudo env FFN_FE100_GUARD_PROCESS_TEST=yes python3 -m unittest test_guard_process
```

The [CP control service](CONTROL-SERVICE.md) now connects the supervisor to
the deployed policy commit barrier. Production admission still needs live
session admission through that service, dynamic policy/route/neighbor attachment
and invalidation, durable counter
transport and safe hardware flow-ID reuse. Kernel accounting leases currently
qualify UDP only; TCP state synchronization is a separate remaining requirement.
