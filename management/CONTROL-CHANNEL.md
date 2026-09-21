# PA5200 control channel

The core's `ffn-controld` owns the management entry point. Platform WebUI and CLI
operations use that gateway; `configd_applier.py` also calls it directly through
the core client. BCM keeps its existing single SDK owner. A telemetry reconnect
does not restart BCM, change PHY speed, assign VIFs, or initialize FE100.

`ffn_cp_agent.py stream` reports BCM port readback, FE100 non-clearing counters,
lookup queue health and the saved policy/session intent summary. The FE100 CSR
description stays local to the CP. Register reads and saved journal entries do
not establish hardware offload readiness. Counter deltas cover the short pair
of samples reported in `sample_interval_seconds`, and assume no reset within
that sample pair.

`ffn_dp_agent.py stream` obtains readiness from the managed DP agent's existing
nonce handshake. It also reads the running VIF owner's Unix socket for revision,
runtime generation, carrier state and counters. It never constructs or reconciles
a VIF owner. An absent socket reports an inactive VIF service; failed socket
readback reports an unknown state. Neither is reported as forwarding.

The Dataplane Status page shows CP/DP connection freshness, FE100 counters and
queue details, saved policy revision/session count, and live VIF forwarding.
Stale observations are labelled historical and their old counters are hidden.
FFN-CLI offers `show platform control`, `agents`, `fe100`, and `control-events`.
These CLI views use the authenticated API, with the same administrator check.

The DP nonce handshake also includes `policy_processing`: the core provider's
current kernel/collector/interface acknowledgment, policy generation, NAT
generation and initial-packet counters. Boot readiness alone does not imply
that Security or NAT is enforcing. OCTEON kernel processing is reported
separately from hardware offload; rule matches alone do not prove successful
reverse NAT or upstream connectivity. An inspection runtime in `off` mode
does not report `inspection-active`.

The journal worker adds these resources:

- `fe100-policy`: status, validate and apply. Apply requires the current revision
  and exact candidate SHA256 digest, and invokes the existing session-drain
  owner. Production flow admission remains blocked pending qualification.
- `hardware`: status, validate and apply. An operation carries revision zero,
  an allowlisted operation, and the current DP boot UUID. The worker executes
  the complete hardware harness operation, including prerequisite checks and
  live readback, within the durable request journal.
- `wan-path`: a bounded WAN1 DHCP Discover/Offer test and recovery, with CP-owned
  cleanup and no lease acquisition. See [WAN qualification](WAN-QUALIFICATION.md).

The WebUI pre-commit policy barrier delegates its drain to controld. Internal
VIF execution still calls the policy owner from inside the worker, avoiding
recursive acquisition of the worker's transaction lock.

Install core `ffn_agent_protocol.py` and `ffn_planed.py` under
`/usr/local/lib/ffn` on CP and DP. Install the CP/DP agent sources under
`/usr/local/sbin`, keeping `ffn-dp-agent.service` running. Existing FE100 lookup
reader dependencies must already be commissioned on CP. Install the updated
management adapters, CLI extension and UI, plus the core gateway code on MP.

On MP run `python3 install-control-channel.py --identity /absolute/ssh/key`.
This requires the PA5200 extension and existing local MP worker configuration.
It merges hardware resources, writes a root-only controld configuration, and
sets manager and credential drop-ins with timestamped backups. It does not
restart services. Reload systemd and restart the MP worker, controld and WebUI
after verifying there are no unresolved or active worker requests. Configd's
platform adapter is reloaded on its next reconcile; this rollout need not
reapply running configuration. No BCM restart or appliance reboot is needed.

Software tests: `test_control_agents`, `test_hardware_control`,
`test_hardware_harness`, `test_configd_applier`, `test_policy_guard`, and
`management/test_ui.cjs`. The core also tests the actual socket, journal and
persistent agent transport without hardware.
