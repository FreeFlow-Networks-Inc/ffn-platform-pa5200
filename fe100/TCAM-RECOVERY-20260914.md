# TCAM synchronization and recovery: verified on PA-5220

## Result

The stalled external TCAM read was recovered without a DDR reset. All263 cfg4
IPv4/IPv6 configuration entries then passed immediate write/readback, a full
readback pass, and an independent verification in a new process. The live MP
harness reports `recovery_required=false`; DDR remains trained. See
`TCAM-RECOVERED-20260914.json` for the combined CP/DP evidence.

This qualifies the recovered TCAM configuration path. It does not establish
physical packet forwarding, TCAM lookup BIST, or session offload. Those flags
remain false. The native post-configuration target0x200 initialization command
was not required to recover configuration access and was not executed.

## Root cause and fixes

Our bring-up omitted the NOP synchronization pulse present in the hash-pinned
owner's `pan_fe100_set_tdi_config` at `0x102b841c..0x102b8478`. The owner sets
`tdi_rst_ctrl.insert_nop`, waits1000 microseconds, then clears it. FFN now also
requires the live `tdi_init_status.ext_tcam_nop_done` bit before table access.

Before recovery: reset control0x4d81, init0x03e00000, IA completion0 and one
pending TLU request. After synchronization: reset control unchanged, init
0x03f00000, IA completion1 and TLU request level0. The pending read completed
but returned zero, showing the prior writes were not valid configuration.
Explicit replay with readback corrected the configuration.

The external receive FIFOs retain alignment data and fluctuate between5/6
entries at idle on this board. They are not outstanding TLU requests. The idle
check now requires drained work queues and FIFO pointer/error checks, accepts
bounded receive-lane occupancy, and rejects full receive FIFOs. Requiring an
exactly constant receive-lane fill produced a false recovery failure even when
the returned TCAM data was correct.

The IA GO bit self-clears after command acceptance. Recovery checks the command
type/target independently of GO and requires explicit completion status before
issuing another request.

## Recovery behavior

`ffn_fe100_tcam_recovery.py --recover --replay` is a bounded commissioning
recovery for the known cfg4 failure, not a general live-session reset facility.
It checks the journal profile, CP boot identity, lack of qualified sessions,
stalled/last-known register address, IA status, clocks and packet counters.
It holds the shared FE100 lock and archives the previous journal. The reset
control pulse preserves all DDR bits. Per-entry replay intent and completion
are durably journaled; every entry must read back before continuing.

Unknown hardware state, a changed CP boot, active observed traffic, a different
pending command, or an interrupted asserted NOP control cause refusal before
new work. An interrupted `recovering` journal requires inspection; it is never
silently converted to success or automatically replayed. An explicit known
failed recovery can be retried after examining its recorded state.

On an already recovered appliance, use verification instead of recovery:

```sh
python3 /usr/local/sbin/ffn_fe100_external_runtime.py --verify
```

Run on CP with the same owner-library LD_LIBRARY_PATH as the installed hardware
status helper. Verification issues only IA read requests, never configuration
writes. It checks every known entry and persists failed verification. It refuses
to overwrite a pending IA transaction. Successful verification does not permit
destructive DDR tests or table reinitialization over the existing journal.

Independent live result:

```json
{"table_configuration_verified":true,"entries_verified":263,
 "recovery_required":false,"session_offload_verified":false}
```

CP evidence:

- `/var/lib/ffn/fe100/external-tables.json`
- `/var/lib/ffn/fe100/clock-init-1789417252735807066.txt` (successful replay)
- `/var/lib/ffn/fe100/clock-init-1789417454668200843.txt` (independent verification)
- `/var/lib/ffn/fe100/external-tables-before-recovery-*.json` (preserved failures)

## Status and validation

The status helper clears recovery only for a verified same-boot journal with
live clock and synchronization prerequisites. It labels configuration readback
as recorded evidence; hardware session qualification remains separate. The MP
harness no longer displays the old TCAM recovery blocker after successful
recovery. No production WebUI routes were mounted by this change.

38 Python tests pass:26 FE100 clock/table/recovery tests and12 harness/API tests.
Coverage includes bounded NOP timeout, control restoration on interruption,
active-traffic rejection, stale journals, receive FIFO faults, pending-command
protection and full263-entry verification. No reboot, DMA reallocation, session
activation, commit or GitHub push was performed.
