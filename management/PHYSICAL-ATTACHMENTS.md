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

## Native CP owner: ffn-portctl

`octeon/native/ffn_portctl.c` is the C implementation of the CP side of this
resource (`status`, `start`, `stop`; JSON payload on stdin, observation on
stdout; exit 2 for a rejected request, 1 for a runtime failure, with the
message on stderr). It keeps the Python owner's contract exactly: the
`/etc/ffn/physical-<port>.json` journal, the BCM lifetime fence
(`<boot_id>:<pid>:<starttime>` of the single ffn_bcmd.py process), the
aggregate-owner refusal, the administrative withdrawal around the first
redirect, the journaled packet-fabric preparation (`/etc/ffn/packet-fabric.json`,
trunk 24 TM_SSP header, eight queues per port), the bounded SDK recipes through
the BCM service (`cint.run` with the staged `/usr/share/broadcom/ffn_bcm_forward_test.c`)
and the same readback rules. The difference that matters: every lock
(`/run/ffn-physical-<port>.lock`, `/run/ffn-faceplate.lock`,
`/run/ffn-forward-test.lock`, `/run/ffn-packet-fabric.lock`) is waited for up
to 15 s instead of one second, because `ffn-copper-link.timer` holds the
faceplate lock about four seconds of every six.

`management/physical_backend.py` runs `/usr/local/sbin/ffn-portctl` on the CP
when the image carries it and falls back to `ffn_physical_forwarding.py`
otherwise. The image build installs it for the CP role. Tests:
`octeon/native/test_portctl.c` (fake BCM service, fake lifetime, in-memory
journal: state machine, fabric preparation, every rejection, parsers), run by
`make test`. Environment overrides for a lab run: `FFN_PORTCTL_ETC`,
`FFN_PORTCTL_RUN`, `FFN_PORTCTL_SCRIPT`, `FFN_PORTCTL_BCM_HOST`,
`FFN_PORTCTL_BCM_PORT`, `FFN_PORTCTL_PROC`.
