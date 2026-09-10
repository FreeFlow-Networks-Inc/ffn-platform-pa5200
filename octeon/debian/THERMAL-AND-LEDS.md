# PA-5220 thermal and LED bring-up, 2026-09-09

The user physically confirmed STS yellow and FAN/TMP green. CP CPLD LED
CSR6/7 were zero before initialization. `ffn_chassis_led.py` writes only
the selected LED field and checks CPLD version and register readback.
STS remains yellow for commissioning; PSU and HA health are not yet wired
to the governor. Front-port LED/activity validation is separate.

Ports 1/3 were observed lit, but their activity indication is unconfirmed.
The user confirmed ports 5/13/23/24 remain dark after both serial-chain tests.
The corrected chain test uses `ffn_led_mmio.c` for volatile 32-bit accesses:
Python buffer/ctypes field access did not preserve upper CMIC control bits.
Known controls 0x20b/0x28b/0x28b were restored, and the corrected test checks
all program words and control values during restoration. SFP/QSFP LED
operation is unresolved; do not infer it from MCU status or traffic counters.

## Hardware references and addressing

Owner files on VM `/mnt/clones/5220-sysroot1-full/opt/dpfs/`:

- `usr/local/lib64/brdagent/cp/libmgmt.so`: CSR6 fields ps1:6,
  ps0:4, fans:2, temp:0; CSR7 status:4, alarm:2, ha:0. Two-bit
  encoding off=0, green=1, yellow=2.
- `usr/local/lib64/ehmon/cp/libfans.so`: two ADT7470 banks, owner
  buses 28/address 0x2c and 29/address 0x2e. Tach pairs 0x2a..0x31,
  PWM bytes 0x32..0x35, RPM=5400000/count. Each register requires its
  own transaction; sequential block reads give incorrect values.
- `usr/local/lib64/ehmon/cp/libthermal.so`: twelve PA-5220 sensors
  and per-sensor ramp/max thresholds. Additional DP1/DP2 sensors are
  for higher models and excluded by the owner's presence mask.
- Owner kernel `boot/vmlinux-3.10.87-oct2-dp`: PCA9548 mux at
  physical I2C1/0x73, channel0 CE thermal, channel2 fan bank 0x2c,
  channel3 fan bank 0x2e. Resolve sysfs channel links, never assume
  the new kernel allocates the old virtual bus numbers.

Kernel mux modules were built against the running CP kernel. The driver
disconnects the mux after every transaction, avoiding address collisions
between NB and CE sensors. No processor reboot was required.

Both controllers returned device/vendor/revision 0x70/0x41/0x02,
matching the [Analog Devices ADT7470 datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/adt7470.pdf).
The datasheet also specifies the dedicated full-speed input; owner fan
initialization clears CPLD CSR17 bit6. On this appliance, leaving that bit
set forces both banks to full PWM. The governor restores it on shutdown.

## Governor and verification

`ffn-thermal.service` is enabled on the CP, requiring the management mux
service. The MP offers `ffn-thermal status`, `auto`, and `full`.

Every cycle reads 12 temperatures and 8 tachometers. Any missing/invalid
reading demands full PWM. The highest normalized temperature demand sets
all fans. Increases are immediate; decreases wait 30 seconds and proceed
in steps of eight PWM counts. The commissioning minimum is 191/255 (75%),
above the owner's minimum of 105/255. This floor is intentionally conservative.

Measured full speed: approximately 11,400–12,100 RPM. At PWM191:
8,900–9,400 RPM, temperatures 29–49 C, no sensor errors. Stopping the
service restored CSR17 bit6, PWM255 on every output and full RPM.
Five policy tests cover every sensor's high threshold, missing readings,
the minimum duty and monotonic response. Systemd provides a 20-second
watchdog and an independent ExecStopPost full-speed action. A deliberate
SIGSTOP test triggered the watchdog, restored full-speed override and
restarted the service; see `THERMAL-WATCHDOG-VALIDATION-20260909.txt`.

The MP sender service supplies its package/core temperatures over pinned
SSH. The CP timestamps receipt locally; missing or older-than-45-second
MP telemetry demands full speed. MP thresholds use hwmon's per-sensor
maximum, with the ramp starting 20 C below it.
Stopping the MP sender was tested on hardware: the CP reported stale MP
temperatures and PWM255, then resumed after sender restoration.

This does not establish recovery from a CP kernel hang or power failure,
nor is it an automatic thermal shutdown implementation. STS and HA health need integration
with the corresponding platform services before indicating overall health.

## Automatic power supply indicators (2026-09-10)

The CP governor samples CPLD CSR0x0a every five seconds. Owner reference
`opt/dpfs/usr/local/lib64/ehmon/cp/libpwrsupply.so` (`5200/cp/ps_monitor.c`)
has two 112-byte descriptors: left supply #1 uses active-low presence mask
0x02 and power-good mask 0x30; right supply #2 uses 0x01 and 0x0c.
The owner's sample routine accepts any asserted bit in the good mask and
gates power-good with presence. We preserve that behavior; these bits are
not independently interpreted as AC/DC telemetry.

Left/index 0 controls PS0 (CSR6 bits5:4); right/index 1 controls PS1
(bits7:6). Present and power-good is green. Missing, not power-good, or
unreadable status is yellow and raises ALARM. This policy treats loss of
either supply as degraded redundancy. Physical slot-to-lamp association
under a single-supply failure still needs a controlled hardware test.

The same governor owns the thermal and PSU ALARM decision, so recovery
of one fault cannot clear the other. PSU errors are reported separately
from thermal errors and do not alter fan demand. A shared lock protects
all LED changes and fan override writes; LED writes verify readback and
preserve STS/HA. Status includes power_csr, power_supplies, and power_errors
in `/run/ffn-thermal.json` and `ffn-thermal status` through the MP.

Ten thermal/PSU policy tests pass, covering healthy supplies, each supply
absent or unpowered, owner partial-mask semantics, unknown reads, recovery,
and thermal alarm retention. No power-control or reset registers are written.

Live deployment readback: power CSR=0x3c (both present/good), LED CSR6=0x55
(PS0, PS1, FAN and TMP green), CSR7=0x20 (STS yellow, ALARM/HA off).
The governor and forwarding service remained active, with no thermal or
PSU errors. This verifies commanded register state; visual lamp confirmation
and physical unplug/reinsert behavior have not yet been tested.
