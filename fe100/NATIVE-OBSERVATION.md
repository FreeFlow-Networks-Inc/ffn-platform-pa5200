# Native Debian FE100 observations

Provision the owner's register map at `/usr/share/ffn/fe100/registers.json`,
root-owned and mode 0644. Generate it with `tools/ffn_fe100_csr.py` against the
owner's matching `libpandp_cp.so.1.0`; the extractor rejects mismatched layouts.
Keep that library and generated metadata out of public code-patch archives.

Register-map consumers, including the base driver CLI, non-clearing lookup
observer and CP agent, use this native location. `FFN_FE100_REGMAP` selects another absolute path when explicitly
configured by the operator. No compatibility root is needed for these reads.
This path change does not remove other bring-up dependencies or enable sessions.

Deploy `ffn_cp_agent.py`, `ffn_fe100.py` and `ffn_fe100_lookup_health.py` together.
The CP also needs the core's `ffn_agent_protocol.py`, `ffn_agent_resources.py`
and `ffn_planed.py` under `/usr/local/lib/ffn`. Configure its pinned SSH observer
in MP controld; the agent does not expose an unauthenticated network listener.
An installed map is not a readiness acknowledgment: PCI decoding, BAR size and
successful non-clearing readback must also pass. Forwarding remains separately
unverified until packet and policy tests pass.

DP agent installation requires `ffn_dp_boot_health.py` beside `ffn_dp_agent.py`
and the same core observation libraries. Stage code in an offline Debian root
when the DP is still in recovery; do not report it as active or replace PID 1
with a chroot process. A verified Debian/systemd boot and an authenticated live
nonce response are required before reporting DP readiness.
