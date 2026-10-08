# FE100 datapath initialisation at boot

`ffn-fe100-init.service` runs `ffn_fe100_init.py run` on the CP once the FE100
links are up (`ffn-fe100-links.service`), as the `fe100-init` stage of the CP
boot graph and of the CP restart path. It brings the FE100 from the reset state
the CE40 personality leaves it in to the state the session owner's readiness
evaluator (`ffn_fe100_live_sessions.py`) accepts, so `show platform fe100
sessions` reports `hardware_initialized: true` in every boot without lab
intervention. Before this stage existed, every boot showed the 27 "lacks
successful initialization in this boot" blockers until someone ran the
commissioning sequence by hand.

## What it runs

Exactly the commissioned order the clean rebuild of 2026-09-24 used, through
the same audited tools and the same owner environment, nothing new touches a
register:

1. the fifteen packet-processing blocks (`ffn_fe100_block_init.py --apply`:
   TLU, PRW, LAG, QMM, LEF, FWD, DFP, ACL, LIF, PAR with the owner parser
   overrides, CFP, EGR, IPQ, NIF, TMI), then the parser program;
2. TCAM PLL and clocks; then, before the TDI DDR is touched, the previous
   generation's external-table journal is archived (`external-tables-archived-
   <boot>-<ns>.json`), but only when the TDI DDR reads the audited reset state,
   i.e. the previous tables are already gone with the memory. A trained, retained
   memory stops the stage with "recovery review required": that is the warm and
   recovery path's decision, not the boot stage's. Then TDI DDR clocks, training,
   the configuration audit and the memory verification;
3. the external lookup tables (`ffn_fe100_external_runtime.py --activate`);
4. FHM and FDT flow memory: clocks, then each channel's training;
5. FCM counter memory: clocks, channel 0, channel 1; then FLU; then SEM.

Thirty-four steps, about four minutes on the PA-5220. Each step's output is
kept in `/var/lib/ffn/fe100/init-<boot>-NN-<step>.log`; the per-boot journal
`init-<boot>.json` records every step, its return code and duration, the
readiness report before and after, and the final stage.

## Idempotence and failure

* A boot whose hardware already reads initialised is left alone (stage
  `already-initialized`); the unit then succeeds without running a tool, which
  is what happens when `ffn-bcmd.service` restarts and pulls the unit with it.
* A step recorded completed in this boot is never repeated: the training and
  initialisation tools refuse replays by design.
* A failed step stops the sequence; the unit fails with the tool's message and
  a later start in the same boot refuses to continue, because recovery from a
  partial training is the documented manual path (`ffn_fe100_flow_memory.py
  --recover-*`, `ffn_fe100_reset.py` in the lab). The next CP boot starts
  clean.
* Only initial table-lock contention is retried (four times, half a second
  apart); any other error is final.

## What it does not do

No production admission, no policy change, no FE100 reset, no BCM change.
Front-port traffic keeps flowing through the DP while it runs; the FE100 only
receives what the switch sends to its two links. The session owner, the
recovery timer and the control service are unchanged; they simply find a
ready device.

`ffn_fe100_init.py status` prints this boot's journal, `plan` the step list.
