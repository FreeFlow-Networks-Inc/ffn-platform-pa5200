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
nor is it an automatic thermal shutdown implementation. STS, PSU and HA health need integration
with the corresponding platform services before indicating overall health.
