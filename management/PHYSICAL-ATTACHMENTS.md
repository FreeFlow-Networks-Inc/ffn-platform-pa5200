# Physical Layer 3 attachments

The MP config adapter creates an independent packet owner for each committed
physical Layer 3 interface. Port 1 retains its existing WAN controller; ports
2–24 use `physical-ports` through controld and the journaled MP worker. The board
module supplies the BCM port mapping. Addresses, profiles and routing remain
customer configuration.

The CP verifies its boot/BCM lifetime, queue allocation and redirect readback.
It withdraws the selected port before allocating queues and journals redirect
writes. An uncertain or foreign redirect is not adopted. The DP owns the port
lock and its TAP; policy bindings verify the DP boot, PID start time, heartbeat,
TAP kind and ifindex. Physical carrier is not a configuration prerequisite.

Configd reconciles the CP and DP attachment before applying the interface address
and management profile, then routes, then coordinated Security/NAT. It checks the
committed XML generation throughout. Detaching disables the CP port before
removing its redirect. Aggregate packet owners retain their independent locks.
Per-port inspection status paths prevent independent owners from overwriting or
removing each other's telemetry.

The image overlay installs the CP helper and DP service template. Installing the
template enables no ports; only committed intent starts it. A failed physical
owner can restart within its current DP boot. A new boot still needs fresh MP
intent. WAN attachment now requires hardware readback, while DHCP and wire probes
remain explicit diagnostic operations.

These attachments forward through the OCTEON software policy provider. They do
not authorize FE100 hardware NAT. The isolated FE100 rewrite checks and production
session admission remain separate.
