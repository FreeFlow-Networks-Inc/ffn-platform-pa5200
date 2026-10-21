# The port table: one owner for every faceplate port

`ffn-portd serve` owns ports 1 through 24 from the moment the dataplane boots,
whether or not anything is configured on them. What each port should be is
one table the MP pushes in full on every commit; the daemon reconciles the
table against what it actually holds and keeps reconciling until they agree.
Nothing on the appliance creates ownership on demand any more: a commit that
enables a port hands a row to an owner that already exists.

This replaces the per-port processes (`ffn-physical@N.service`,
`ffn-wan-attachment.service`), the per-group aggregate process the MP
supervisor started over ssh, and the three MP backends that drove them
(`physical_backend.py`, `wan_backend.py`, `aggregate_backend.py`), with one
daemon and one MP resource, `ports`. The status documents, the per-port
command lines and the aggregate supervisor's JSON-line contract are unchanged,
so the policy bindings, the network owner, the WebUI and the CLI read what
they read before.

## The table

```json
{"boot_id": "<this DP boot>", "generation": "<sha256 of the running config>",
 "ports": {"1": {"mode": "l3"}, "2": {"mode": "disabled"},
           "21": {"mode": "aggregate", "group": "ae1"}, "22": {"mode": "aggregate", "group": "ae1"},
           "...": {"mode": "disabled"}}}
```

Every port 1 to 24 has a row; a row the MP leaves out is `disabled`. Modes:

| mode | the daemon holds | derived on the MP from |
|---|---|---|
| `disabled` | nothing: the TAP the network owner made stays down | no `layer3` on `ethernet1/N`, or `link-state down`, or an empty layer3 (no address and no management profile) |
| `l3` | the port's native packet owner on its TAP (the WAN owner `wan1` on port 1, `physical-N` otherwise), its workers, its carrier policy and its status document | `layer3` with an address or a management profile |
| `aggregate` | a reservation: no physical owner may take the port; the aggregate lease that names the port activates it as a member | `aggregate-group aeN` |

The MP's `ports` resource derives the table from the running configuration
(`ports_backend.desired_table`) and pushes it with `ffn-portd table apply`;
the daemon keeps the last accepted table in `/run/ffn-portd-table.json` and
restores it when it is restarted inside the same boot, so a daemon restart
re-attaches every port without the MP. After a DP reboot the table is empty
until the lifecycle replay pushes it: the same moment the attachments were
made before, with one request instead of one per port.

## The daemon

One process, `ffn-portd.service` (`Requires=ffn-network.service`,
`Restart=always`). Its loop polls the control socket, every client, every
aggregate's trunk control socket and every running helper, then once a tick:

* reconciles the table: a row that says `l3` without a running owner is
  attached (port lock, native owner on the TAP, workers, status document); a
  row that says `disabled` with a running owner is detached; a failed attach
  (the TAP not yet there, a lock held by another owner) is recorded with its
  reason in the table status and retried every two seconds;
* serves each attached port exactly as `physical serve` did (inspection
  policy, local addresses, FE100 return bindings, carrier lease, status);
* runs each aggregate exactly as `aggregate serve` did (LACP engine, member
  gates, trunk control frames, network helpers, DHCP client, status rows);
* checks every native owner for a worker fault without sleeping
  (`ffn_packet_fault`, additive in the library) and publishes
  `/run/ffn-portd.json`, the table status, every second.

At start it recovers what a previous instance left behind in `/run`: aggregate
intent files whose owner is gone are torn down through the teardown helper,
stale physical status documents are removed, and the saved table is restored.
The dataplane watchdog timer (`aggregate sweep`) stays as the backstop for a
daemon that stops publishing; its absence is logged, not fatal.

Locks keep their meaning inside one process: `flock` conflicts between open
file descriptions, so an aggregate holding port 21 still excludes an `l3`
attach of port 21, and the `physical start` probe from the command line still
proves the locks are free.

## The socket

`/run/ffn-portd.sock`, root only, JSON lines. A connection sends requests and
reads one reply per request, except a lease, which turns the connection into
the stream the MP supervisor speaks.

| request | reply |
|---|---|
| `{"op": "status"}` | the table status: every port's desired and actual state, carrier, owner document path, reason when not as desired; every aggregate's status row |
| `{"op": "table", "boot_id", "generation", "ports"}` | `{"ok", "revision", "rejected": {"21": "member of live aggregate ae1"}}`: the rows that conflict with a live aggregate lease are refused, the rest replace the table |
| `{"op": "attach", "port", "boot_id"}` / `{"op": "detach", ...}` | one row changed to `l3` / `disabled`, then the port's status document once it is running (or gone), as `physical start` / `stop` replied |
| `{"op": "links", ...}` | the faceplate observation, as the `links` verb takes it on stdin; recorded for the carrier lease and the route tracker |
| `{"op": "lease", "intent": {...}}` then observation lines | the aggregate owner's status rows, one per second, until the connection closes or the observation lease lapses; then the owner withdraws its gates, tears its network down and is gone, as the stdin contract did |

A lease is accepted when every member's row is `aggregate` for that group or
`disabled`; a table pushed afterwards cannot turn a live member into `l3`
(the row is rejected; deactivate the group first, as the network owner's LAG
guard already demands).

## The command lines (unchanged for their callers)

* `ffn-portd aggregate serve`: the relay the MP supervisor reaches over ssh
  through `ffn_aggregate_runtime.py serve`. It opens a lease with the intent
  line from stdin, forwards every further stdin line to the socket and every
  status row back to stdout, and exits when either side closes.
* `ffn-portd physical start|stop|status --port N`: `attach` / `detach` /
  `status` on the socket, with the boot identity check on stdin as before.
* `ffn-portd links`: delivered to the socket when the daemon answers, written
  to `/run/ffn-port-links.json` and `/run/ffn-route-links.json` directly when
  it does not, so the route tracker's observation never waits for the daemon.
* `ffn-portd table apply|status`: the MP's `ports` resource.
* `ffn-portd aggregate sweep|recover|revision`: unchanged.

## The MP resource: `ports`

`ports_backend.py` serves `status`, `validate` and `apply`:

* `apply {"operation": "sync", "running_revision", "revision"}` derives the
  table, starts the CP owner of every port that becomes `l3` (`ffn-portctl
  start` for ports 2 to 24, the WAN forwarding owner for port 1 with its
  fabric readback and policy-barrier drain as before), pushes the table,
  waits for the attached rows to run, and withdraws the CP owner of a port
  that did not come up or is no longer `l3`. One request per commit.
* `apply {"operation": "attach"|"detach", "port", ...}` keeps the per-port
  path for the WebUI's physical-port and WAN pages.
* `apply {"operation": "activate"|"deactivate"|"recover", "group", ...}`
  is the aggregate lifecycle, served by the same module (the supervisor
  process and its CP coordination are unchanged).
* `status` returns the DP table status, the CP owner of every `l3` port and
  the aggregate report the `aggregates` resource returned.

The old resource names (`physical-ports`, `wan-path`, `aggregates`) are
served by the same module for the WebUI and the CLI; `configd_applier.py`
uses `ports` alone, and port 1 is a row like any other: the `wan-path`
special case is gone.
