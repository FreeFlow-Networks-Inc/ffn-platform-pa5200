# Committed interface reconciliation

The Linux alias-based ethernet applier cannot control PA-5200 ASIC ports.
Install `configd_applier.py` in the selected platform directory and the
`ffn-configd-platform.conf` drop-in on `ffn-configd.service`. Apply the core
`image/install-configd-platform.py` installer to the deployed configd script;
it checks the expected engine layout, compiles the result and keeps a backup.
The service drop-in also exposes the current core helpers to the legacy configd
entrypoint through `PYTHONPATH`. Installing the adapter file alone is not enough:
verify that the service has `FFN_CONFIG_PLATFORM` set and that the engine contains
the selected-platform reconciliation hook. After deployment, reload systemd and
restart configd, then check the new apply-status timestamp and agent readback.

The hook runs after XML validation and before the empty-diff optimization.
Consequently an old last-applied snapshot cannot hide unapplied platform
settings. Platform-owned ethernet/aggregate paths do not fall through to MP
Linux interface appliers. Failed reconciliation must not advance last-applied.
Both raised exceptions and reported reconciliation errors stop generic dispatch.
An agent handshake alone does not prove the network controller, port mapping, or
forwarding attachment is commissioned; missing runtime components remain errors.

Supported now: faceplate link-state up/down/auto and basic commissioned L3 port
addresses/MTU. Down disables the physical port and its DP namespace endpoint,
removing old addresses. The MP daemon remains the sole execution path.
Unspecified front ports default to disabled. Supported parts may apply while unsupported
parts remain errors; this is not a cross-node atomic transaction.

Aggregates/LACP require their committed attachment to be acknowledged by the
platform agents. Unsupported interface modes and options return explicit apply
failures. DP configuration readback is recorded separately from physical
forwarding attachment; inactive attachment remains an error. This adapter does
not invent Linux aliases or advertise an MP bond as a hardware aggregate.

The installed legacy control daemon's explicit reapply notification may time
out; committed XML changes still trigger the existing inotify watcher. A warm
configd restart runs reconciliation as well. Allow enough time for serialized
MP/CP/DP operations and inspect the new apply-status timestamp before reporting
success. A status from an earlier apply is not confirmation of the latest one.
