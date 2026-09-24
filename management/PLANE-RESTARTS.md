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

Install separate MP systemd owners:
`ffn-pa5200-restart-cp.service` and `ffn-pa5200-restart-dp.service`. Each must be
`Type=oneshot`, `RemainAfterExit=no`, with an error-propagating ExecStart.
Materialize `ffn-pa5200-restart@.service` with the corresponding role in place of
`%i`. Install `plane_restart_owner.py`, `native_plane_boot.py` and
`plane_lifecycle.py` in `/opt/ffn-platforms/pa5200-management` on the MP.
Do not point these units at the old combined CP/DP boot service or manual
recovery scripts. Their exit status must reflect the entire boot operation.

On the CP, install `native_plane_boot.py`, `plane_restart_node.py` and
`dp_restart_agent.py` under `/usr/local/sbin`, the updated `ffn_dpsh2.py` in that
directory, and `ffn-native-dp-boot.service` under `/etc/systemd/system`.
The relay needs `ffn_agent_protocol.py` in `/usr/local/lib/ffn`. Use the native
BCM service drop-in `ffn-bcmd-native-restart.conf` only with a native Debian BCM
installation. Install persistent MP-to-CP and CP-to-DP transport units pointing
to their actual native executables. Reload systemd on both processors.

The MP owner pauses the active config reconciler, aggregate workers and link
reconciliation while operating. CP restart records the active hardware services,
sets fans to full PWM, stops CP-to-DP transport, and restores cooling before
switch and interface services. DP restart is owned by CP systemd so an SSH
disconnect cannot orphan the boot operation. Both paths stop transport writers,
refuse residual BAR mappings, and hold the corresponding hardware reset lock.
After the selected kernel returns, the owner checks transport service health,
verifies the other processor's boot ID is unchanged, and resumes the previously
active MP reconcilers. Reconciliation completion and traffic recovery require
separate verification; a successful restart is not a forwarding test.

Provision root-owned, non-group/world-writable JSON boot profiles at
`/etc/ffn/plane-boot/cp.json` on MP and `/etc/ffn/plane-boot/dp.json` on CP.
Each schema-1 profile contains `role`, `pci`, `devnum`, `cores`, `fdt`, `extra`,
`kernel`, `bootloader`, `tools`, `transport`, and optional `environment`.
Kernel, bootloader and tool entries contain absolute `path` and `sha256`;
the kernel also contains `notes_sha256`, the SHA-256 of its ELF `.notes` section.
Tools are `reset`, `boot`, `csr`, `stage`, plus `load` for DP. Additional pinned
transport executables can be included. Transport contains its systemd `unit`
and exact process `argv`. Image paths, credentials, core allocation and topology
belong in these deployment profiles, never in WebUI input or source defaults.
Preflight verifies PCI enumeration, all pinned files, and the running kernel's
notes. It refuses to change the selected image during a restart.

Run `plane_restart_owner.py cp preflight` and `plane_restart_owner.py dp preflight`
on MP before enabling either role. These are read-only checks, not hardware
restart qualification. Provision root-owned, non-group/world-writable
`/etc/ffn-ngfw/pa5200-restarts.json`:

```json
{"schema":1,"roles":{"cp":{"enabled":true,"timeout":1200},"dp":{"enabled":true,"timeout":1200}}}
```

Enable only roles with verified profiles and installed owners. Timeout (30–1200 seconds) bounds each owner wait
and subsequent agent acknowledgment wait. Configure fresh CP and DP agents in
controld. Missing owners, stale identities or a missing DP agent disable the
corresponding button with a reason. No legacy reset fallback is attempted.

The global restart lock serializes operations across both processors. Requests
carry the observed revision, boot identity and explicit outage acknowledgment.
Workers survive WebUI restarts, persist outcome/history, recheck identity before
dispatch, and require a new boot ID plus a fresh processor acknowledgement. A
recovery mailbox agent can acknowledge a reboot while explicitly reporting
`ready=false`, `runtime=recovery`, and `forwarding_verified=false`. Such jobs
report recovery mode in the UI; they must not claim a forwarding-ready dataplane.
The relay reports no CPU utilization rather than substituting CP counters.
A
systemctl timeout does not imply that the boot owner stopped; an active owner
continues blocking further restart requests. No reset is retried automatically.
Hardware command deadlines never kill an in-flight vendor BAR transaction:
the owner retains its lock until that child exits, then reports failure.
Owner journals retain the paused services and boot observations for recovery.

Deploy `plane_lifecycle.py` and `static/plane-lifecycle.js` with the updated
platform UI/CLI and MP resource registration. Register the CP-hosted
`dp_restart_agent.py` relay as the sole PA-5200 DP observation agent in controld
when the DP uses the recovery mailbox. Installation itself performs no processor
restart. A first live restart must still be checked for service restoration,
LACP acknowledgements, policy reconciliation and forwarding before considering
that hardware/image combination qualified.
