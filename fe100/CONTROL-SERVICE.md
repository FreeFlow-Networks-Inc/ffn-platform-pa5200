# CP FE100 control service

`ffn-fe100-control.service` is the persistent CP policy-barrier owner. It is
enabled in CP images and has been deployed on the PA-5220. The existing MP
control daemon remains the management authority; CLI and WebUI do not acquire
hardware ownership. The CP service currently exposes only status, policy
replacement and reconciliation. Production hardware traffic admission remains
disabled.

The worker holds the policy SQLite journal lock for its lifetime. Requests use
a root-owned, mode-0600 Unix sequenced-packet socket. Both ends verify kernel
peer credentials; request UUIDs bind responses to individual calls. Frames,
connection waits and silent clients are bounded. Revision conflicts do not
replay a replacement, including when the original response was lost.

The independent parent supervises worker progress and its complete cgroup.
Systemd restarts a failed parent. Startup first kills an orphaned worker group,
then reconciles exact durable session intent before accepting requests. Native
recovery failure blocks restart admission. The existing recovery timer routes
through the same owner instead of opening another journal instance.

Starting the service durably creates `control-service-required` in the FE100
state directory. After that, policy CLI calls must use the socket. Failure or
absence of the service never falls back to another one-shot hardware owner.
This requirement remains across restarts. Recovery invoked by the supervisor
uses the direct adapter only after its old worker group has been fenced.

The MP commit barrier now checks the exact candidate digest, next revision,
zero remaining sessions, completed recovery, disabled admission and unchanged
CP owner identity. Both its direct transport and the MP control-daemon gateway
use these checks. A restarted CP owner between status and replacement causes
the barrier to reject that acknowledgement; a fresh attempt is required.
No configuration is applied by these barrier calls.

## Verification

Host and MIPS64 CP tests cover persistent ownership, restart, lost responses,
malformed/oversized/silent clients, permissions, revision conflicts and refusal
to bypass a required but unavailable service. The management tests also reject
wrong digests, revisions, enabled admission and changed owner identities.

On the deployed CP, freezing the worker caused independent withdrawal and
automatic restart in approximately 33 seconds. Killing its supervisor caused
orphan cleanup and restart in approximately 7 seconds. Both retained the policy
revision and digest; the old cgroup was gone before the replacement owner was
accepted. These service tests had an empty production hardware-session journal.
Physical deletion of active flows is separately qualified by the isolated tests
in `SUPERVISED-WITHDRAWAL-EVIDENCE.json`; this service test is not evidence of
production traffic forwarding.

Both MP barrier transports succeeded against the installed service using the
existing running configuration, without changing it. The management API was
reloaded to use the stronger acknowledgement checks. WAN/aggregate packet
processes and the MP control daemon were not restarted. No appliance reboot
is required for this component.
