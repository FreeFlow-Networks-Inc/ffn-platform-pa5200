# OCTEON readiness and inline engines

Install `ffn_dp_agent.py` in `/usr/local/sbin` and `ffn-dp-agent.service` in
`/etc/systemd/system` on the dataplane Linux node, then enable/start the service.
Install `ffn-dp-agent-mp` as `/usr/local/sbin/ffn-dp-agent` on management. The
existing pinned CP proxy and DP host keys must already be provisioned.

The agent exposes a root-only Unix socket, not a TCP listener. Each status call
performs a fresh protocol-v1 nonce exchange through the existing SSH channel.
Responses include the kernel boot ID, detected OCTEON CPU, available inline
detectors, and fresh inspection runtime status. SSH failure, missing agent, or
invalid response is an unavailable resource, never a cached ready result.
The platform's authenticated `/api/pa5200/dataplane` and selected-provider
`/api/system/runtime/dataplane` endpoints expose it; the controls page shows the
handshake. A ready agent does not mean NIF initialization, hardware table replay,
forwarding, or an inspection policy has been activated.

The shared C adapter now supports `credit_card` (Luhn), `ssn` (format/plausibility),
and `api_key` (AWS access-key ID format), alongside the existing literal match.
Inspection policy accepts optional `detectors`, a unique list of these names.
Existing policies without it retain their behavior. All selected detectors use
the policy's alert/block action and ingress port scope. Reload allocates a new
engine before replacing the previous working policy. An older library rejects
additional detectors rather than silently omitting them.

These scanners execute on the OCTEON CPU in the existing DP fabric process,
before frames enter the DP network namespace. They do not use a verified Cavium
regex/crypto accelerator and are not full IPS, application identification, TLS
decryption, or stream DLP. Scanning is bounded to 2048 payload bytes; fragments,
unsupported protocols and malformed frames retain the existing separately
counted pass behavior. MP still relays physical port traffic in the commissioning
fabric. No MP security engine should be disabled on the strength of this feature.

Deployment of the agent/library does not enable a blocking policy or start the
fabric. Hardware queue/table replay must be completed before starting forwarding.
Use the WebUI policy editor and verify matching runtime revision and counters
before claiming that configured inspection is active.

Validation: `make -C octeon/dpfwd inline-test`,
`python3 octeon/debian/test_dp_agent.py`, and the management API/UI tests.
Run adapter tests on actual MIPS64 with `FFN_INLINE_LIB` pointing to the freshly
built shared library before replacing the installed binary. Back up existing
library, inspection module, platform controller/UI and unit files before rollout.
