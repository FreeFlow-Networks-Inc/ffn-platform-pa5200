# PA-5220 management extension

Optional controls for the FFN-NGFW core manager. No discovery or hardware IO at
module load. Install only on the PA-5220 MP after the Debian controllers have been
commissioned. Copy this directory to `/opt/ffn-platforms/pa5200-management`, owned
by root and not writable by unprivileged users.

The core must include `opt/ffn_extensions.py` and its manager/WebUI hooks.
Select the extension using a systemd drop-in for `ffn-manager-v2.service`:

```ini
[Service]
Environment=FFN_PLATFORM_EXTENSION=/opt/ffn-platforms/pa5200-management
```

Reload systemd and restart only the manager. To remove it, remove this environment
setting and restart the manager; forwarding and thermal services remain separate.
No platform-selection file or code is installed on generic FFN hosts.

## Control surface

All endpoints below use the core bearer authentication and live account checks.

| Endpoint under `/api/pa5200` | Function |
|---|---|
| GET `/status` | Parallel component status, partial errors and qualified capabilities |
| GET `/{network,overlay,inspection,thermal,chassis,fabric}` | Controller diagnostics |
| POST `/network/patch` | Revisioned per-port L2/L3, VRFs, static routes, ECMP and policy rules |
| POST `/network/lookup` | Read-only IPv4/IPv6 route lookup, optional source/VRF |
| POST `/overlay/set` | Revisioned VXLAN, Geneve, GRE/IPIP and MACsec link configuration |
| POST `/inspection/set` | Revisioned literal payload off/alert/block configuration |
| POST `/thermal/{auto,full}` | Automatic cooling or full-speed fans; empty JSON object |

Mutations require admin/superuser. API payloads follow the controller schemas in
`../octeon/debian/ADVANCED-ROUTING.md`, `VIRTUAL-NETWORK.md`, and `INLINE-ANALYSIS.md`.
Requests are capped at 64 KiB; process stdout/stderr at 1 MiB each. Commands are
fixed, use no local shell, and never interpolate request data into SSH commands.
Status timeout is 25 seconds and configuration timeout is 90 seconds. Timeout
does not prove remote rollback: refresh status before retrying. Audit entries
omit payload text, addresses and secrets.

The WebUI appears under Dashboard only when this package is explicitly selected.
It provides port mode/VLAN/address/VRF forms, inspection policy controls,
advanced JSON editors, route lookup, PSU/LED state and fan controls. Configuration
is immediate and persistent through the platform controllers, not core XML commit.
The editor preserves observed revisions and prevents a second write from stale
state. Read-only accounts can view diagnostics but cannot submit changes.

Physical forwarding remains qualified for ports 1/3/5/13 and MTU1500 via the
software relay. No complete FE100/BCM hardware offload, production FRR service or
MACsec key management is claimed by this extension.

Tests: `python -m unittest discover -s management -p test_control.py` using the
core's FastAPI test dependencies; `node management/test_ui.cjs` for rendered
controls, read-only behavior, text escaping and submitted port configuration.

## Live verification, 2026-09-10

Installed and selected on the MP at 172.19.0.70. The running core received only
the generic extension loader and UI hooks, preserving its other deployed code.
Manager and fabric services remain active. All six controller status requests
succeeded through the authenticated API. Unauthenticated status returned 401;
a stale network revision returned 409; live route lookup and automatic fan
control returned 200. Network configuration remained at revision59, overlay at
revision12, inspection at revision12. No physical port configuration was changed.

Eleven platform API tests and the UI behavior test passed. Four generic loader
tests plus the core UI isolation test verify that unselected hosts load no
platform code or assets. Existing manager security (9), hardware API (3), auth
script and hardware UI tests also passed individually. Browser visual inspection
of the new page has not been performed.

Rollback on this MP: remove only
`/etc/systemd/system/ffn-manager-v2.service.d/50-platform-extension.conf`, reload
systemd and restart the manager to unload the module. Pre-integration manager
and HTML copies have `.before-extensions-20260910` suffixes if the generic hooks
also need reverting. This does not stop forwarding or cooling controllers.
