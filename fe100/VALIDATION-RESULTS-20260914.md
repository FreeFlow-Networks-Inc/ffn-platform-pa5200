# PA-5220 TCAM and recovery validation

Result: **TCAM configuration/recovery validation passed on the appliance.**
Physical packet forwarding and FE100 session offload are **not yet validated**.

## Live CP tests

`validate_tcam.py` executed three full readback passes in fresh processes:
263 entries per pass,789 total. All matched the cfg4 IPv4/IPv6 configuration.
Native MMIO traces contained exactly the expected address/read-command pairs,
with zero configuration-data writes, reset writes or denied accesses.

Both live guard tests passed:

- DDR memory testing over the existing external-table journal was refused.
- Replaying recovery over the verified configuration was refused.

The guards produced no MMIO writes and left the journal unchanged. DDR, PLL,
clock and reset registers were identical before and after validation. Training,
DDR clocks, TCAM clocks and NOP synchronization remained ready;
`recovery_required` remained false. Validation did not inject a new hardware
failure or reset the appliance.

Full evidence is in `TCAM-VALIDATION-20260914.json`. The same report is on CP:
`/var/lib/ffn/fe100/tcam-validation-1789417768266888165.json`.

## Regression and DP tests

- 26 FE100 clock/table/recovery Python tests passed on the build VM.
- 12 harness/API tests passed on the MP using isolated test dependencies.
- 20 packet-init, transport, session-lifecycle and journal tests passed on the
 actual big-endian MIPS64 DP.
- The host C MMIO allowlist regression passed.
- The MIPS64 queue/descriptor test binary reported zero failures on the DP.
 Its hardware operations are test doubles (`unavailable: no SDK at build time`);
 this is not a real DMA or packet-transmit qualification.

Total:58 Python tests passed, plus both C test binaries. Native DMA pools were
observed in their existing prepared state, not reallocated or destructively
retested. Their capacities remain512,570 and1024 buffers with `dma_error=0`.
SSO memory, PKO memory/queue topology and PKI microcode remain prepared.

## Unvalidated networking paths

Live DP status still reports `pki_enabled=0`, `pko_enabled=0`, `pki_active=0`.
The internal BGX link is up, but the root network namespace contains only
`lo`, `sit0` and the management TAP `ffndp0`; no physical packet netdevice is
commissioned. The FE100 native session adapter remains unqualified.

Consequently this run did not perform front-port packet forwarding, routing,
hardware lookup-hit/miss tests, session installation, aging or removal. Those
require completion of PKI input mapping/consumers, PKO transmission/credits,
and the native session adapter. The status evidence is in
`VALIDATION-PLANE-STATUS-20260914.json`.

No reboot, production WebUI change, commit or GitHub push was performed.
