# Lifetime-bound packet qualification

The control owner has two packet gates that no inventory or journal can
satisfy: the front-port gate (`PolicyOwner.qualified`: an installed session
forwards through the switch steering on this silicon) and the NAT gate
(`nat_qualified` and the adapter's `nat_offload_verified`: the FE100 rewrites
tuples and checksums exactly). Both were constants before 2026-10-08, so every
session evaluated as blocked by them regardless of the September proofs.

`ffn_fe100_qualification.py` keeps the audited result of
[the harness](NAT-PACKET-QUALIFICATION.md) where the owner trusts it:

* **Lifetime.** Every audited case names the CP boot and the BCM owner epoch
  (`ffn_copper_forwarding.epoch`) it ran in; the record is accepted only for
  the live pair, and every read re-checks it. A reboot or a switch daemon
  restart retires the record, and the owner goes stale exactly as for a
  changed attachment: drain, then blocked.
* **Storage.** `/run/ffn-fe100/qualification.json`, mode 0600 on tmpfs. It
  cannot outlive the boot, and the FE100 personality is reloaded every boot.
* **Scope.** `external-wire` is the complete eight-case matrix on the
  front5/front13 loop. `internal-loop` is the cable-free MAC loop on an
  unconfigured port: both protocols, address-plus-port translation, forward
  and reverse. Either scope lifts both gates; the status names which one.
* **Not activation.** `production_admission` stays false in every auditor
  summary, and the owner's `activate` remains the operator's explicit
  operation. The record changes what the dry-run evaluator and `admit_pair`
  would accept; it installs nothing.

## Operating it

```sh
# on the MP, after the runs documented in NAT-PACKET-QUALIFICATION.md
python3 /usr/local/sbin/validate_nat_results.py --internal R1.json R2.json R3.json R4.json \
  | ssh -F /etc/ffn-ngfw/ssh-cp.conf ffn-cp \
      python3 /usr/local/sbin/ffn_fe100_qualification.py submit --scope internal-loop
# on the CP
python3 /usr/local/sbin/ffn_fe100_qualification.py show
```

`submit` goes through the control owner (`qualify` operation on the control
socket); the owner records it and reconciles. The owner's `status` carries
`qualification` and reports `capabilities.nat_packet_qualification` from it.

## What it does not cover

The adapter's readiness still requires the live hardware summary (external
clocks, pipeline faults, `offload_verified`) and the DP physical transport
flag; the record supplies only `nat_offload_verified`. Aggregate hardware
egress selection and policy activation are separate gates.
