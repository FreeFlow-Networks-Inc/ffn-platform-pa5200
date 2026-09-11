# Single MP control owner

The WebUI and FFN-CLI are clients of the authenticated management API. Its
platform `Controller` only sends RPC requests to `/run/ffn-plane-mp/control.sock`.
There is no direct-execution fallback if that socket is unavailable.

`ffn-plane@mp` owns serialized hardware requests and the durable SQLite outcome
journal. The platform's `planes/mp.json` supplies fixed executable adapters for
faceplate, network, overlays, inspection, thermal control and hardware status.
`hardware_backend.py` is executed only by the daemon; it communicates with the
existing CP/DP helpers over their pinned transports. Hardware validation and
readback remain on the owning node. Root administrators can still use recovery
tools locally; they are not an alternate WebUI/CLI control path.

Install the core `ffn_planed.py` at `/usr/local/lib/ffn/ffn_planed.py` and beside
the manager with `ffn_plane_api.py`; install the core `ffn-plane@.service` unit.
Install this platform's `control.py`, `hardware_backend.py`, `daemon_backend.py`,
and `cli_extension.py` in `/opt/ffn-platforms/pa5200-management`. Also install the
platform's `octeon/debian/ffn_inspection.py` there for shared policy validation.
Install `planes/mp.json` at `/etc/ffn/planes/mp.json`, then enable/start
`ffn-plane@mp`. Verify socket status before restarting management with the new
client. The daemon must be available before enabling these API clients.

`install-cli-hook.py` installs a version-checked hook into the existing FFN-CLI,
retaining a backup. It keeps the CLI's existing authenticated session and API
authorization:

```
show platform
show platform faceplate
show platform dataplane
request platform faceplate set '{"revision":123,"port":1,"enabled":false}'
```

Use the observed revision; 123 is illustrative. Changes are immediate platform
operations, separate from candidate/commit. Responses identify the MP request ID
and trace. Unknown outcomes are journaled and block further changes to that
resource until operator reconciliation through the daemon's result/resolve
protocol. Rejected validation does not execute the mutation.

This integration covers selected PA-5200 runtime controls. It does not replace
the independent core configuration database/commit daemon. Legacy BCM port
enable URLs also route through MP control and persist the mapped faceplate
state. Legacy loopback writes are unavailable until a commissioned daemon
adapter exists; they cannot bypass the MP. Read-only diagnostic APIs remain.
