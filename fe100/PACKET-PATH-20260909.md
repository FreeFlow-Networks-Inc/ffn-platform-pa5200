# PA-5220 packet-path development, 9 September 2026

The full FE100 system-port forwarding lab passes on the owner's PA-5220,
with native Debian MIPS64 big-endian CP/DP and NIF loopback disabled.
DP Ethernet reception and firewall offload remain unverified.

## Hardware evidence

* Front5→cable→front13: 3,000 returned, zero missing/duplicate/corrupt.
  See `../bcm/FORWARDING-VALIDATION-20260909.txt`.
* NIF Ethernet loopback: 3,000 returned with the same checks passing.
  See `NIF-LOOPBACK-VALIDATION-20260909.txt`.
* Full MP/BCM8 → BCM3 → NIF → parser/LIF → PRW/TMI → BCM20 → BCM8/MP:
  3,000 returned, zero missing/duplicate/corrupt, loopback disabled.
  See [payload evidence](LIF-FORWARDING-VALIDATION-20260909.txt) and
  [counter evidence](LIF-FORWARDING-COUNTERS-20260909.json).

The final test recorded 3,004 NIF receives, LIF/TLU hits, LIF SYSPORT
forwards, PRW outputs and TMI channel-0 transmissions. Four background
frames accompany the 3,000 UUID/sequence-checked payloads. Ingress exception
code 0 and implementation code 0 increased by 3,004; no error counter
changed. DFP/FWD/LEF/QMM/LAG bypass counters increased for this SYSPORT
operation. This test does not exercise ACL or route lookups.

Tests use three bursts of 1,000 packets at 0.5 ms pacing, not line-rate
or endurance measurements.

## Driver-derived fixes

References are local on the VM under `/mnt/clones/5220-sysroot1-full`.
See [driver ABI notes](DRIVER-REFERENCE-20260909.md).

The owner `fe100_cfg1` is a 2,812-byte initialized object. The initializer
copies it into a private buffer with PA-5220 usecase 1 and TLU partition
4/2 overrides. Owner parser JSON and NIF receive mapping
(switch=0, device=0, port=8) → logical port 8 are loaded. Initializers
succeeded for TLU, PRW, LAG, QMM, LEF, FWD, DFP, ACL, LIF, PAR, CFP,
EGR, IPQ, NIF and TMI.

`ffn_fe100_spm.py --apply` installed and read back 32 owner mappings:
slot 0, logical ports 8–15, four priorities → physical system port 8.
TMI capture showed the outgoing Jericho ITMH destination change from 15
(first word 01000f00) to 8 (01000800).

The MP ixgbe NIC discards RAW FE100 returns with receive-length errors
because a 32-byte FE100 message header precedes the original Ethernet
packet. The frame probe's explicit `--rx-all` option temporarily enables
reception, waits two seconds for NIC recovery, and restores the prior
feature state on normal exit, SIGINT or SIGTERM. CRC errors did not increase.
The returned marker offset is 74: 32-byte envelope + Ethernet 14 + IPv4/UDP 28.

Initially this path returned LIF-miss exception packets. The scoped
`ffn_fe100_lif_lab.py --apply` entry now matches only logical ingress
port 8 and selects forwarding type 5 (SYSPORT), destination 8, in-LIF 8.
Table 0/index 0 was empty (owner NOTFOUND=3). Insert and exact readback
passed. The final test produced LIF hits and ingress exception code zero.

## Lab operation and retained state

Owner calls use external timeouts and a process lock. The MMIO adapter
selects one 32-KiB block and only permits known registers. LIF access uses
an explicit table selector instead of PAN's absent process device object.
All four adapters build with -Wall -Wextra -Werror as MIPS64 BE libraries.
Owner binaries and firmware are not included in this repository.

BCM allocation attaches all eight queues, uses two remote cores and an
explicit ingress credit profile. Run allocation once per switch startup.
`prepare-forward-test.py` renders allocation or route-toggle recipes.

After testing, nif-disable completed successfully: temporary BCM ingress
lab routes are disabled. Queue allocations, FE100 packet configuration,
SPM mappings and the scoped LIF entry remain in RAM for continued work.
They are not installed at boot. Inspect before reuse; the LIF installer
refuses to overwrite a valid slot.

CP and DP report systemd running. BCM, front-port, copper and FE100 link
services are active. NIF PCS is 0x10, TMI 0x15fff, both PLLs 1. MP rx-all
is restored off; both MDIO write gates are N. NIF loopback is off and TMI
capture is restored.

To clear FE100 state, quiesce BCM routes, run the owner two-write reset
helper, and restart ffn-fe100-links.service. Reinitialize packet blocks
and tables after reset. Do not reset DP with MP/CP transport writers active.

Remaining work: DP receive/transmit and FE100 message handling, firewall
policy/flow programming, payload tests on all front-port paths, repeatable
packet startup and sustained traffic testing.
