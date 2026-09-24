# PA-5220 management extension

Optional controls for the FFN-NGFW core manager. No discovery or hardware IO at
module load. Install only on the PA-5220 MP after the Debian controllers have been
commissioned. Copy this directory to `/opt/ffn-platforms/pa5200-management`, owned
by root and not writable by unprivileged users.

The core must include `opt/ffn_extensions.py`, `opt/ffn_policy_barrier.py`, and
their manager/WebUI hooks. Keep `policy_guard.py` alongside `control.py`.
Select the extension using a systemd drop-in for `ffn-manager-v2.service`:

```ini
[Service]
Environment=FFN_PLATFORM_EXTENSION=/opt/ffn-platforms/pa5200-management
```

Reload systemd and restart only the manager. To remove it, remove this environment
setting and restart the manager; forwarding and thermal services remain separate.
No platform-selection file or code is installed on generic FFN hosts.

## Optional hardware policy barrier

The manifest declares `policy_barrier_version: 1`. Before an XML configuration
commit, the selected extension drains CP-owned hardware sessions and leaves
admission blocked. Failure prevents the running-configuration write. This is
separate from the immediate controller mutation endpoints described below.

Install `fe100/ffn_fe100_policy_control.py`, `ffn_fe100_policy.py`,
`ffn_fe100_sessions.py`, `ffn_fe100_nat.py`, and `ffn_fe100_journal.py` in the CP's
`/usr/local/sbin`, alongside the commissioned live-session adapter dependencies.
The owner stores its journal in `/var/lib/ffn/fe100/policy-sessions.sqlite3`.
Debian's SQLite library is required; the MP guard pins it to avoid the obsolete
library in the vendor search path. The CLI exposes only `status` and `replace`
(drain), with JSON on stdin. It does not enable production flow admission.

Physical front 5/13 FE100 egress and separate tests of both distinct-port
directions passed. Concurrent policy-pair admission and continuous invalidation
for all direct-controller writers remain unqualified. See
[`FRONT-EGRESS-POLICY-20260915.md`](../fe100/FRONT-EGRESS-POLICY-20260915.md).

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
| GET `/lacp` | Saved LACP groups, activation capability and kernel partner/member diagnostics |
| POST `/lacp/{set,activate,deactivate}` | Save profiles separately from explicit group activation; current relay rejects activation |
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

LACP installation: copy `../octeon/debian/ffn_lacp.py` alongside `ffn_network.py`
on the DP and install `ffn-lacp-mp` as executable `/usr/local/sbin/ffn-lacp` on
the MP. See `../octeon/debian/LACP.md` for the schema and qualification limits.
No LACP profile or member assignment is created by installation.

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

## VIF assignment controls

The optional **Virtual Interfaces** page and `/api/system/runtime/vifs` API
connect MP assignment controls to the DP TAP/PKI/PKO transport. Configure a
front-port/VLAN binding, L2 bridge membership or L3 addresses/VRF; start forwarding
separately. No assignments are installed automatically. The current commissioned
port allowlist is 5 and 13. See
[`VIF-INTEGRATION-20260915.md`](../octeon/debian/VIF-INTEGRATION-20260915.md)
for installation, recovery, references, test evidence and qualification limits.
# Core runtime and page integration

This module requires the core's `runtime_api_version: 1` and `registerPage`
hooks. Install the updated FFN-NGFW core first, then select this management
directory through `FFN_PLATFORM_EXTENSION` and restart the manager. The manifest
declares Device > OCTEON platform. Merely checking out this submodule does not
enable the page or any probes.

The UI uses `/api/system/runtime`, bound exclusively to this selected module.
Legacy `/api/pa5200` endpoints remain for existing clients. Both paths use the
same allowlisted controllers and revision checks. Core object storage is separate
from OCTEON policy enforcement; no generic host firewall fallback is used here.

Inspection changes now check the live DP revision after persistence, for a bounded
eight-second observation period. `activation: active` requires a running dataplane
with the accepted revision and no reload error. Otherwise the response reports
pending, failed, superseded, or unknown. This observation does not roll back a
saved policy, retry a write, restart forwarding, or change inspection semantics.

Read-only session planning is exposed by `show platform fe100 sessions [json]`.
See [session planning and isolated NAT qualification](../fe100/SESSION-PLANNING.md)
for deployment, freshness checks and the remaining hardware admission gates.

## Live bring-up acknowledgement

The final `ffn_oct.py --plan` step (also consumed by the WebUI) queries
`ffn-controld` for nonce-validated CP and DP agent observations. It requires
all processor instances expected by the detected chassis profile, distinct
boot identities, matching report identities, ready agents, and unexpired
observations. Expiry uses the MP's monotonic receive age, not processor wall
clocks that may be unsynchronized during boot.

Control handoff also requires a read-only `plane-lifecycle/status` round trip
through controld and its MP execution worker. A restart in progress, missing
worker, changed boot identity, or stale observation keeps the step waiting.
No configuration is committed and no processor is restarted by this check.
The plan's ready count describes boot prerequisites and control-channel
readiness; physical forwarding and policy enforcement have separate checks.
