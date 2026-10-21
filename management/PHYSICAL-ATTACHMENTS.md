# Physical Layer 3 attachments

Every faceplate port has one owner from the moment the dataplane boots:
`ffn-portd serve` (PORT-TABLE.md). On every commit the MP config adapter
derives what each of the 24 ports should be, keeps the CP owner of every
layer-3 port in step (port 1's WAN forwarding owner included) and pushes the
whole table to the daemon in one `ports` request; the daemon attaches and
detaches to match and keeps retrying what it could not do, with the reason in
its table status. The board module supplies the BCM port mapping. Addresses,
profiles and routing remain customer configuration.

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

## Native DP owner: ffn-portd

`octeon/native/ffn_portd.c` is the C owner of every port's packet path on the
DP, one daemon under `ffn-portd.service` with the table the MP pushes
(`ffn-portd table apply|status`, PORT-TABLE.md). An `l3` row runs what
`ffn_wan_runtime.py serve` and `ffn_physical_runtime.py serve` ran as their
own processes, under the same owner names and documents (port 1: owner `wan1`,
`/run/ffn-fabric.json`; port N: owner `physical-N`,
`/run/ffn-physical-N-status.json`), so the policy bindings and the network
owner read what they read before (owner, pid, process_start, boot_id, ports,
interfaces, updated_monotonic, `carrier`); `pid` and `process_start` are the
daemon's. The MP's per-port command lines `status`, `start` and `stop` with
the DP boot identity on stdin still exist (`ffn_wan_runtime.py`,
`ffn_physical_runtime.py` exec `ffn-portd physical ... --port N`); `start` and
`stop` change one row of the table through the daemon's socket. Copper ports
1..4 are ordinary faceplate ports to this owner; the switch port behind a
copper front port comes from the commissioned profile
`/etc/ffn/vif-copper.json` when copper-identify corrected it. A port whose
carrier policy is `observed` (copper 2..4 by default; `/etc/ffn/port-carrier.json`
overrides per port or by `default`) raises TAP carrier and delivers frames only
while the latest `ffn-portd links` delivery is within its lease and reports the
port's PHY, MAC and data path up at one speed; the MP's `ffn-port-links.timer`
(`management/port-link-poll.py`) makes that delivery from the `faceplate`
resource. A port with policy `always` keeps carrier as the Python owner did.

## The MP resource: ports

`management/ports_backend.py` serves `ports`: `status` (the DP table status,
the CP owner of every layer-3 row, the aggregate report) and `apply`
`{operation: sync, running_revision, revision}`, the commit's one request. The
sync derives the table (`desired_table`: `aggregate-group` rows reserve a port
for its group; a `layer3` with an address or a management profile, not
`link-state down`, is `l3`; everything else is `disabled`), starts the CP owner
of every port that becomes `l3` (`ffn-portctl start` for ports 2..24; for port
1 the WAN forwarding owner with its fabric readback and a `recover` when the
redirect belongs to another lifetime, after the policy-barrier drain), pushes
the table, waits for the `l3` rows to attach, and withdraws the CP owner of a
port that did not come up or is no longer `l3`. Per-port `attach`/`detach`,
the WAN qualification `probe` and the aggregate `activate`/`deactivate`/
`recover` are served by the same resource through the modules that always
served them (`physical_backend.py`, `wan_backend.py`,
`aggregate_activation.py`), and the `physical-ports`, `wan-path` and
`aggregates` resource names remain for the WebUI and the CLI.
`configd_applier.py` uses `ports` alone; port 1 is a row like any other.

## Link-tracked routes: the observation, its two writers, the check

A static route committed on a physical or aggregate interface is link-tracked:
the core's route tracker on the DP (`ffn_static_routes.py`, one tick per
second) installs it only while this platform's adapter
(`octeon/debian/ffn_network.py` `route_link`) says the port is up, and the
adapter reads `/run/ffn-route-links.json`: `{boot_id, revision, links: {pN:
bool}, observed}`. An observation from another DP boot or older than 60 s
withdraws every tracked route with `hardware-link-observation-stale`.

That file now has two writers, and the tracker depends on neither alone:

* `ffn-portd links` on the DP (`octeon/native/ffn_portd.c`,
  `route_links_record`) writes it from the faceplate rows the MP's
  `ffn-port-links.timer` delivers every three seconds for the owners' carrier
  lease; a port is up when its row says `available`, `enabled` and `link`.
  The delivery's reply carries `route_links: true` when the record was
  written, so `journalctl -u ffn-port-links` shows it.
* `ffn-route-link-feed.service` on the MP (`route_link_feed.py`, every five
  seconds) asks the control daemon for `route-links refresh`
  (`daemon_backend.py`), which reads the same faceplate rows and sends them
  through the network owner's `health` verb, which writes the same record.

Until 2026-10-20 the MP feed was the only writer. A core patch install stopped
`ffn-controld`; `PartOf=` propagated the stop to the feed, nothing started it
again (`Restart=` does not apply to an explicit stop, and the feed's
`WantedBy=ffn-controld.service` was only declared, never linked), the
observation went stale and the DP withdrew both default routes for fifteen
minutes. Nothing reported it. The fixes, in order of what they remove:

1. the DP writer above, so a dead MP feed no longer withdraws routes;
2. `install-control-channel.py` makes the `ffn-controld.service.wants` links
   for every unit that declares `WantedBy=ffn-controld.service` (the route
   feed, the session feed, the interface-service tunnel), so the daemon pulls
   them back in when it starts; the core installer also starts the units its
   stop propagated to;
3. the `tracked-routes status` daemon resource (`daemon_backend.py`,
   `tracked_routes_report`): one ssh round trip reads the tracker's state, the
   observation, the network journal and the DP's clock and boot, and reports
   each tracked route's installed/reason. The core's convergence report carries
   it as the `tracked-routes` subsystem: `failed` when a route is withdrawn for
   a stale or missing observation, when the tracker has stopped ticking (30 s)
   or when a route cannot be installed as committed; `pending` while a route
   waits for its link, path or aggregate member, or the tracker has not yet
   reported for this DP boot or the current network revision; `converged` when
   every tracked route is installed. `failed` pages through ffn-alerts; the
   report's details carry the observation's age and the MP feed unit's state,
   and the console's reapply restarts that feed.

Retirement of the MP feed: once the DP writer has been seen keeping the
observation fresh on the appliance through a control-daemon stop (stop
`ffn-route-link-feed.service` by hand, watch `tracked-routes status` stay
converged and the observation age stay under four seconds for a few minutes),
the feed unit, `route_link_feed.py`, the `route-links refresh` command and the
network owner's `health` verb can go; the check and its `feed` field stay. Not
done yet: the DP writer has been proven on x86 (`test_portd_links.py`,
`test-portd-linux.py`), not on the box.

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
