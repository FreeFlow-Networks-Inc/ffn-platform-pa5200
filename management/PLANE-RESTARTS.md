# Independent processor restart

System > Hardware & Runtime > Dataplane & Engines exposes Control Plane and Data Plane restart buttons through
the `plane-lifecycle` MP worker resource. CLI equivalents are:

```
show platform processors
request platform restart cp acknowledge-outage
request platform restart dp acknowledge-outage
```

This restarts the selected running-image boot path. It does not activate a staged
image, change boot selection, or reset the management plane. CP restart can
interrupt the DP control transport and traffic even though only CP is targeted.

## Commissioning

An administrator must first install and qualify separate MP systemd owners:
`ffn-pa5200-restart-cp.service` and `ffn-pa5200-restart-dp.service`. Each must be
`Type=oneshot`, `RemainAfterExit=no`, with a bounded, error-propagating ExecStart.
Do not point these units at the old combined CP/DP boot service or manual
recovery scripts. Their exit status must reflect the entire boot operation.

The CP owner must drain hardware work and stop BAR writers before resetting CP,
restore transport, I2C/cooling and switch control, and avoid resetting DP. The DP
owner must drain forwarding, stop its BAR writers, reset only DP, restore its
transport and replay committed interface/LACP and policy state. Owners must use
the administrator-selected installed image and retain existing reset locks.
Image paths, credentials and topology belong in deployment configuration.

After hardware qualification, provision root-owned, non-group/world-writable
`/etc/ffn-ngfw/pa5200-restarts.json`:

```json
{"schema":1,"roles":{"cp":{"enabled":true,"timeout":600},"dp":{"enabled":true,"timeout":600}}}
```

Enable only commissioned roles. Timeout (30–1200 seconds) bounds each owner wait
and subsequent agent acknowledgment wait. Configure fresh CP and DP agents in
controld. Missing owners, stale identities or a missing DP agent disable the
corresponding button with a reason. No legacy reset fallback is attempted.

The global restart lock serializes operations across both processors. Requests
carry the observed revision, boot identity and explicit outage acknowledgment.
Workers survive WebUI restarts, persist outcome/history, recheck identity before
dispatch, and require a new boot ID plus a fresh ready agent for success. A
systemctl timeout does not imply that the boot owner stopped; an active owner
continues blocking further restart requests. No reset is retried automatically.
Traffic recovery remains a separate qualification from agent readiness.

Deploy `plane_lifecycle.py` and `static/plane-lifecycle.js` with the updated
platform UI/CLI and MP resource registration. Installation itself performs no
processor restart. The current appliance has no commissioned independent boot
owners, so its controls report that prerequisite instead of offering unsafe resets.
