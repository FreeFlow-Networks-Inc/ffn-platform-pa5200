# Automatic aggregate packet-fabric recovery

The MP aggregate supervisor restores an activated aggregate using running XML
and fresh CP/DP observations. The WebUI and CLI use the same controller status
and activation operations. Candidate configuration is never used for recovery.

1. Validate the current configuration and fence the previous owner token.
2. Withdraw the previous CP ownership and recover DP attachments only within
   their original boot lifetime. A new BCM epoch requires proving old redirects
   and offload trunks are absent before withdrawing that owner's member links.
3. Drain the FE100 policy barrier before packet setup.
4. Load the installed `ffn_dp_packet_init` module, bring up the disabled internal
   MAC through the existing guarded SDK helper, then run missing PKI, DMA,
   SSO, PKO memory, PKO queues and trunk-registration stages in order. Start the
   trunk and verify its engines and descriptor queue. Never reset an active or
   faulted engine automatically.
5. Prepare BCM packet queues for the hardware's internal trunk and the committed
   aggregate members. The SDK supplies queue/connector IDs and port/core mapping.
   The board's queue geometry and internal header format are platform constants.
6. Start new CP/DP aggregate ownership and wait for configuration ACK and LACP
   negotiation before exposing the parent or VLAN attachment as applied.

The CP journal is `/etc/ffn/packet-fabric.json`. It records the BCM epoch,
pending operation, allocation results and resource IDs. Existing complete queue
bundles are reused. A partial/uncertain operation blocks automatic allocation
until reconciled; a new BCM epoch is checked against live hardware before old
journal state is replaced. Allocations are retained for reuse when an aggregate
is stopped. No automatic resource deletion or SDK restart is performed.
`/etc/ffn/packet-fabric-last-result.json` retains the last mutating SDK response
for diagnosis, including return codes and allocated IDs.

The runtime restores prerequisites, not a throughput qualification. Scheduler
attachment readback is unavailable on this SDK; successful SDK calls, queue
readback, LACP and packet tests provide distinct levels of verification.

## Install on the dataplane

Build `ffn_dp_packet_init.ko` against the exact deployed kernel using
`octeon/debian/build-dp-packet-init.sh`. Stage the resulting module with the
installer and Python helpers from `octeon/debian`, then run on the DP as root:

```sh
./install-dp-packet-runtime.sh ./ffn_dp_packet_init.ko
```

The installer verifies architecture, module name and kernel release, installs
under the current kernel's module directory and runs `depmod`. It does not load
or replace a running module. Recovery loads the installed module on demand.

Install `ffn_packet_fabric.py` and the updated `ffn_aggregate_hardware.py` on the
CP under `/usr/local/sbin`; install the updated aggregate MP helpers and
`ffn-aggregate@.service` in their existing locations, then run `systemctl daemon-reload`.
Activate through the existing WebUI or `request platform aggregate <name> activate`.
Activation persists across MP boots. Explicit Stop/Recover disables automatic
startup. Existing deployments can enable the supervisor for an already saved,
activated aggregate; do not enable arbitrary groups without committed intent.

Observe recovery with `show platform aggregates`. Its `activation.recovery_stage`
explains the current prerequisite; errors withhold readiness. Successful recovery
publishes a new owner token and a matching DP configuration revision. Addresses,
VLANs, management profiles and speeds are always read from committed settings.
