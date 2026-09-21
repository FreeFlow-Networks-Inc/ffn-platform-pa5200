# FE100 policy session recovery

The CP runs `ffn-fe100-recovery.timer`, starting after boot and repeating 15
seconds after each service invocation finishes. The oneshot service is bounded
to 45 seconds, with a five-second stop deadline. It reconciles the existing
SQLite policy/session journal under the exclusive journal lock. Hardware access,
when journaled sessions exist, additionally requires the FE100 table lock and
the commissioned native adapter's prerequisites.

Recovery removes only exact journal-owned entries and verifies their absence.
An ownership conflict, failed delete, inaccessible adapter, or incomplete
readback preserves pending intent and blocks admission for a later retry.
There is no table flush, chip reset, initialization, route change or policy
activation. The policy revision and digest do not change during recovery.
An empty, blocked journal does not open the hardware adapter.

`PolicyOwner` also exposes periodic reconciliation for a future continuous
qualified owner: changed attachment bindings, failed qualification, and
observer errors drain existing pairs without waiting for another admission.
A newly constructed owner cannot adopt a persisted active acknowledgement;
it must reconcile and receive a new applied-policy acknowledgement first.
The deployed CLI deliberately offers only status, replace/drain, and local
reconcile. General production flow admission remains unavailable.

## Installation on a commissioned PA-5200 CP

Install the FE100 Python modules in `/usr/local/sbin`, including
`ffn_fe100_policy.py`, `ffn_fe100_policy_control.py`, and
`ffn_fe100_recovery.py`. Install the service and timer from `octeon/debian`
in `/etc/systemd/system`. The service pins Debian SQLite ahead of vendor
libraries. This installation presumes the existing journal/session adapter
dependencies; it does not commission uninitialized hardware.

```sh
systemctl daemon-reload
systemctl start ffn-fe100-recovery.service
systemctl enable --now ffn-fe100-recovery.timer
systemctl status ffn-fe100-recovery.timer
journalctl -u ffn-fe100-recovery.service -n 20
```

The job atomically publishes `/var/lib/ffn/fe100/policy-recovery.json` with
`drained`, `busy`, or `blocked` outcomes. CP telemetry includes this under
`policy.recovery`. A drain is verified only when the report belongs to this CP
boot, is at most 90 seconds old, matches the journal revision, and the current
journal is blocked and empty. It means known journal-owned sessions are
absent; it does not certify the whole ASIC's tables or production forwarding.
Consumers must also honor the enclosing agent's freshness.

FFN-CLI `show platform fe100` includes driver, policy, and recovery observations
through MP controld. No new direct WebUI or CLI hardware-control path is added.

## Validation

The deployment passed 60 FE100 lifecycle/encoding tests, nine CP/DP observation
tests, and six CLI tests. These cover restart fencing, periodic invalidation,
failed deletion retry, journal lock contention, idempotent empty recovery,
atomic reports, and stale boot/generation rejection. The live CP timer reported
a successful empty drain, and MP controld received a fresh recovery report.
Neither plane rebooted. Production admission stayed blocked.

Remaining qualification includes continuous trusted policy/session events,
session aging, simultaneous bidirectional front-port tests, NAT/inspection
handoff, and sustained forwarding under load. This recovery change does not
claim those capabilities.
