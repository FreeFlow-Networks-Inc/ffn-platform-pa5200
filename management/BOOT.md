# MP-owned PA-5220 boot profile

The root `boot.json` is consumed by the core MP hardware startup worker. It
matches a Linux x86 MP with the CP's `177d:9700` endpoint and a PA-5220 DMI model,
or an explicitly enrolled MP identity on an already commissioned appliance.
Do not infer the processor model from a vendor ID alone. This profile does not
claim startup support for the multiple-DP PA-5250/5260/5280 layouts.

Startup order is the existing MP OCTEON CP/DP boot owner, CP I2C and cooling,
BCM, front ports, copper PHY, FE100 links, saved faceplate state, CP/DP execution
workers and the MP execution worker. Platform code remains in this submodule;
the core provides generic discovery matching, serialization and status.

Fresh CP observations must acknowledge OCTEON, a responding BCM owner and actual
FE100 driver readback. DP observations must acknowledge OCTEON and its real
systemd root, not a chroot or unfinished initramfs. This does not enable FE100
session admission. Configd subsequently applies the customer's saved interfaces,
aggregates, routes, Security and NAT through the existing control path.

Core installation and transport provisioning are documented in
`FFN-NGFW/docs/hardware-boot.md`. This profile names already commissioned services;
it does not install vendor files or manufacture credentials. Read `show platform
boot` through the normal console session to inspect MP startup state.

The deployment must use the commissioned combined `ffn-octeon.service` boot path
that waits for both Debian processors. Existing reset safeguards and local image
selection remain in that unit. The MP installer disables its independent boot
enablement without stopping it, then enables the ordered MP hardware worker.
CP-owned startup units remain idempotent under `systemctl start`; active units
and successful oneshots are observed rather than restarted.
