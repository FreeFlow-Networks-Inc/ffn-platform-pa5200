# FE100 retained-state commissioning

This path qualifies retained FE100 memory for isolated lab work after a CP
reboot. It never marks old training journals as current, resets hardware,
retrains memory, or authorizes production session admission.

`ffn_fe100_warm.py --qualify-lab` holds the FE100 table lock and requires an
empty hardware session table, one intact historical commissioning chain, the
pinned native owner ABI, compatible current controller state, and fresh PHY
readback from both channels of FHM, FDT and FCM. Current-boot initialization
attempts must use their normal recovery path; this mechanism cannot override
failed or partial training in the current boot.

The resulting root-owned journal binds the current boot and monotonic time,
profile, source-file hashes and stable controller registers. Its 600-second
grant applies only to explicit commissioning sessions in reserved lab zones
4093/4094. Production adapters retain the current-boot initialization and
packet-qualification gates. Requalify immediately before a bounded lab run;
an expired grant cannot authorize new session operations.

## Read-only diagnostic boundary

The separate `libffn-fe100-diagnostic.so` permits only bounded PHY read commands
and their address register writes. It rejects PHY writes, IA payload writes,
reset writes, invalid addresses and other device targets. The installed
production flow-memory adapter is not replaced by this diagnostic library.

FCM read coordinates were checked against `dphy_reg_rd` at `0x102ded68` in the
VM sysroot's `opt/dpfs/usr/local/lib64/libpandp_cp.so.1.0`, SHA256
`b57227a460144c8c2545fc2e268b31f475ef72ac2a6f1d457387ab46d842c3e9`.
Calibration checks follow static inspection of `check_init_cal_status` at
`0x102e4898`; FFN does not invoke its unbounded polling loop. The corresponding
PDT source is `opt/dpfs/usr/share/pdt/fe100.py`.

UMCTL status comparison retains APB state and sticky-stall faults. Queue
credits, instantaneous stall and CAM-empty fields change during ordinary
operation and are not calibration evidence. Comparative eye widths from the
sysroot's PA-5260 sample remain warnings, not PA-5220 pass/fail thresholds.

## BCM lab ownership

`ffn_fe100_bcm_lab.py` executes fixed recipes through the existing BCM daemon.
Each recipe stages and restores the shared script while holding the same CP
lock used by WAN and aggregate owners. Multi-operation preparation releases
that shared lock between SDK calls; a separate lab lock serializes lab work.

Queue preparation observes existing resources and allocates only missing
eight-queue bundles for the isolated front5/front13 loop and its internal
FE100/capture destinations. It neither replaces production queues nor changes
the occupied trunk header. A durable pending journal prevents blind retry after
an ambiguous allocation. Prepared queues remain allocated for the BCM lifetime;
the lab does not attempt unsafe deletion of shared scheduler resources.

Baseline redirect intent is journaled before any change, restored from actual
observed state, and fenced to the same BCM process lifetime. QMAP queue IDs are
read through `BCM_GPORT_UCAST_QUEUE_GROUP_QID_GET`, never copied from a previous
boot's allocation order. The MP harness aborts if a production aggregate becomes
unhealthy or changes owners between test phases.

Install the helper on CP beside the FE100 modules and stage the reviewed
`bcm/ffn_bcm_forward_test.c` as
`/usr/local/share/ffn/fe100-lab/ffn_bcm_forward_test.c`. The CP needs the existing
`ffn_faceplate`, `ffn_copper_forwarding` and `ffn_packet_fabric` modules.
The MP receives `validate_front_sessions.py`; the DP keeps its packet probe.
Build the diagnostic library with `fe100/build-adapters.sh` and install only
that new library under `/usr/local/lib/ffn` for this diagnostic update.

## Live observations, 2026-09-21

All six memory channels passed fresh calibration/error/readback checks with
zero MMIO scope faults. No hardware reset or memory retraining was performed.
The first packet test exposed missing post-reboot BCM lab queues. After scoped
preparation, four of four baseline frames returned across the physical loop.

The address-NAT trial then produced four session hits and four hardware drops
in their respective phases. Its hit-phase packets missed QMAP and arrived at
diagnostic capture unchanged: **hardware NAT did not qualify**. All temporary
FE100 entries, BCM rules, baseline redirects and capture settings were restored.
At that checkpoint, the translated-address QMAP fixture and dynamic queue
selection were implemented but their physical NAT result remained unverified.

Live tests also coincided with aggregate-owner recoveries. The initial lab lock
covered too many SDK calls; it was narrowed to individual calls. Testing was
stopped after another recovery, and cleanup completed. Bounded heartbeat retries
now distinguish temporary lock contention from lease, SDK and ownership errors;
the MP still rejects stale replies under its existing five-second limit.
Aggregate failures are logged durably instead of disappearing when the next
restart replaces the status file. These control changes are installed without
restarting the active owners and take effect at their next normal start.

Final checks found the WAN gateway reachable, both aggregate members
distributing, and the existing Security/NAT collector healthy. Running and
candidate configuration hashes remained unchanged. Complete physical NAT
qualification and sustained control-plane coexistence are still required before
production FE100 admission; the existing OCTEON kernel provider remains active.

## Follow-up, 2026-09-22

The translated-address QMAP key has now produced exact physical NAT rewrites.
See [NAT packet qualification](NAT-PACKET-QUALIFICATION.md) for the TCP/UDP
matrix and its limits. These results remain separate from production admission.

One initial test still starved the production BCM lock and caused aggregate
owner recovery. The lab now yields for 100ms outside the shared lock between
SDK operations; the subsequent qualification sequence retained the same active
aggregate owner. Packet capture also uses a larger per-socket receive buffer
and rejects any reported capture loss. No global buffer settings were changed.
