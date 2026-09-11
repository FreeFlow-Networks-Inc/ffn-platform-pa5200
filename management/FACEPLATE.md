# Faceplate administrative control

Network > Faceplate Ports maps ethernet1/1 through ethernet1/24 to the verified
BCM faceplate list. Internal NIF/fabric ports and the extra SDK port 12 cannot be
addressed through this API. Link and speed are observed, not user-writable.
Admin enable/disable does not configure VLANs or L3 forwarding; use Dataplane
Interfaces for the currently commissioned forwarding ports.

Install `ffn_faceplate.py` on CP at `/usr/local/sbin`, and
`ffn-faceplate-mp` on MP as `/usr/local/sbin/ffn-faceplate`. The platform API
uses fixed SSH commands and permits writes only for administrators. It never
accepts raw SDK commands. BCM `port.set` is followed by `port.list` readback.

Requests contain a current observed revision, faceplate port number and boolean
enabled state. Stale snapshots are rejected. Desired state is journaled on CP
at `/etc/ffn/faceplate.json`; uncertain operations block subsequent writes.
After inspecting hardware, a local administrator can run
`python3 /usr/local/sbin/ffn_faceplate.py resolve` on CP to accept the observed
state of the pending port. It is intentionally not an automatic retry.

Enable `ffn-faceplate-restore.service` on CP to restore verified saved states
after `ffn-front-ports.service` during boot. Initializer restarts outside boot
can reset hardware states; refresh and reapply saved settings explicitly.
The observed revision detects changes through other tools but does not lock
those external tools out of the ASIC. Do not edit the same port concurrently
through an independent SDK client.

Tests cover mapping, bounds, stale revisions, verified persistence, unknown
outcomes and role-gated UI. A live same-state write/readback verifies SDK access
without cycling a link. Speed/autonegotiation/FEC control and disruptive link
cycling are not certified by that test.
