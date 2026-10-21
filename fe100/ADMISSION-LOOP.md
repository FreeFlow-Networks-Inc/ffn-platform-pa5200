# FE100 admission loop

`ffn_fe100_admission_loop.py` is the driver that was missing between the owners
of the hardware offload path. The CP control service holds the ordered
observation inventory (`Observations`), the policy owner installs paired
session entries (`PolicyOwner.admit_pair`), the lifecycle keeps bounded leases
and revokes them (`SessionLifecycle`), and the offload policy decides which
rows deserve a slot now (`ffn_fe100_offload_policy.should_offload`, the default
written 2026-10-21: long-lived, two-way, fat or fast flows, bounded slots,
back-off after failures, a higher bar when the table is three quarters full).
Until now nothing connected them: the supervised evaluator reported what blocked
each row and installed nothing, which is what the admission evaluation's
"ordered session lifecycle feed is not connected" reason names.

## Modes

`observe` (the default, and what the control service runs today): every pass
runs the gate (`admissible`) and the policy over the live inventory and keeps a
shadow lease table with the lifecycle's idle (60 s) and maximum (300 s)
lifetimes. The status counters say what the policy would have admitted, renewed
and withdrawn on real traffic, before any table is written. This is how the
policy's thresholds are judged on the appliance.

`admit`: the loop drives a real `SessionLifecycle`. On the first pass for a
producer it starts the producer lease on the owner's barrier (`start(producer,
revision, digest)`, which activates the policy generation), then per pass sends
a heartbeat, one `open` per admitted row carrying the paired request the dry-run
evaluator already builds (`ffn_fe100_admission.request_for`), a `refresh` while
the row keeps moving, and a `close` when the row closes. Any lifecycle error
fences the lease and the loop resynchronizes on the next pass. Failed
admissions are counted into the policy's back-off and the row is not retried
for 30 s.

`off`: nothing runs.

The mode comes from `/etc/ffn/fe100-admission.json` (`{"mode": "observe"}`);
an absent file means `observe`. `admit` is refused and falls back to `observe`
while the owner has no commissioned directional path owner (`PolicyOwner.paths`
is `None`), because `admit_pair` cannot succeed without it.

## What still gates real admission

The loop closes the control-plane gap. Three things still stand between
`admit` mode and a session in the chip, in the order they bite:

1. **The control service's drain invariant.** `ffn_fe100_controld.serve`
   requires `drained()` (phase blocked, zero sessions, admission disabled) every
   two seconds and on every request; an activated policy generation fails it
   and the worker exits. The invariant was written for the observation-only
   era and must become: drained after `replace` (a commit) and after recovery,
   active with sessions allowed between commits. The MP commit barrier keeps
   its exact check; a commit always drains.
2. **Commissioned owners behind `bindings()` and `paths()`.** The policy owner
   is constructed with `bindings=lambda: {}` and no path owner. The attachment
   owner (`AttachmentOwner`, LIF/LEF per interface) and the path owner
   (`PathOwner`, next-hop and source-MAC leases) exist and are table-proven;
   wiring them in is the commissioning work listed by the admission
   evaluation (zone and miss path, LIF/LEF and QMAP with the next-hop index in
   word 2, next-hop and source-MAC leases, counter and exception handoff).
3. **Steering.** Production ports 16 (WAN) and 32/33 (ae1 members) forward to
   the DP trunk today; the WAN port was inline for an eight-hour window on
   2026-10-20 with the DP return path and the owners' return bindings and
   passed. Steering them inline for good, and the LAG members with either
   hardware egress selection or a fixed member per session, is a maintenance
   window each.

The DP side of an admitted session (the conntrack lease that keeps the entry
liberal and alive, the FIN/RST punts re-injected into the kernel path, aging
from the chip's counter records) is specified in `commissioning/README.md` and
the first-session evidence; the loop's `open`/`close` events are where that
lease is taken and released.

## Reading it on the box

`fe100-policy status` through the MP plane daemon (or `show platform fe100
sessions json`) carries `admission_loop`: mode, slots, current leases, the
counters (`admitted`, `renewed`, `closed:<reason>`, `failed`, `examined`,
`passes`), the last pass's reasons, and in `admit` mode the lifecycle status.
