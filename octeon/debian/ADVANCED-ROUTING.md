# Advanced routing commissioning, 2026-09-10

The MP now manages IPv4/IPv6 static routes, default gateways, blackhole
routes, weighted ECMP, multiple Linux VRFs and source-based routing policies
through `ffn-network`. These are software routing functions on the OCTEON DP.
The physical relay remains limited to front ports 1, 3, 5 and 13 at MTU1500.
No FE100/BCM routing or session offload is implied.

## MP configuration

Fetch `ffn-network status` before an update. `patch` requires the current
revision. The `ports` object merges individual port settings; `routes`,
`vrfs` and `rules`, when supplied, replace their respective complete collections.
Omitted collections remain unchanged. The old ports-only format still works.

Example configuration fragment for two virtual routers (supply a current
revision before applying):

```json
{
  "revision": 59,
  "vrfs": {"vrf-blue": 1001, "vrf-red": 1002},
  "ports": {
    "p5": {"mode": "l3", "vrf": "vrf-blue", "addresses": ["198.18.1.1/24"]},
    "p13": {"mode": "l3", "vrf": "vrf-red", "addresses": ["198.18.2.1/24"]}
  },
  "routes": [
    {"dst": "0.0.0.0/0", "via": "198.18.1.2", "dev": "p5", "table": 1001, "metric": 100}
  ]
}
```

VRF names are `vrf-` followed by lowercase letters, digits or hyphens, with
15 characters maximum. Table IDs are unique integers 1000..65535. The
controller permits at most64 VRFs; simultaneous capacity at that limit is
not tested. Ports without `vrf` use the default routing domain, table254.
Overlapping local addresses are allowed across VRFs but rejected within one.
Both address families get an unreachable fallback in each VRF table to
prevent ordinary unresolved lookups falling through into the main table.

Route destinations must be canonical prefixes (`0.0.0.0/0` or `::/0` for a
default). Unicast routes require an L3 `dev` in the selected routing domain.
A `via` must be directly reachable on that port, with IPv6 link-local next
hops supported. Metrics are positive integers below the reserved VRF
fallback metric4278198272. A blackhole route has `type: "blackhole"` and
no interface or gateway.

ECMP replaces a route's top-level `via`/`dev` with `nexthops`, containing
2..16 distinct `{ "dev": "p5", "via": "198.18.1.2", "weight": 1 }`
objects. Weights range1..256; each next hop must belong to the route's VRF.
Current tests verify kernel installation and route selection, not physical
traffic distribution, failover, or throughput across independent uplinks.

A policy rule contains `from` (canonical source prefix), `iif` (L3 ingress
port), `table` (configured virtual router), `priority` (100..999), and
optional `to` prefix of the same address family. Priorities are unique per
family; occupied kernel priorities are rejected. Policies run before the
kernel's ordinary VRF rule. Explicitly selecting another VRF's table is
therefore intentional inter-VRF routing; isolation is the default without
such policies. The lab verified policy selection and removal for IPv4/IPv6.

`status` includes main IPv4/IPv6 routes, per-VRF routes, and both policy rule
sets. For a FIB lookup, pipe JSON such as
`{"dst":"198.18.1.2"}` to `ffn-network lookup`. Optional `src` and `vrf`
select the source and virtual router. A lookup is not a packet-delivery test.

Changes share the existing network lock, revision checks and persisted
configuration. Failed additions and persistence errors roll back applied
changes. Route additions do not replace unmanaged colliding routes. Remove
dependent routes/policies before changing their ports. VRF deletion refuses
tables that still contain routes beyond the controller's fallback entries.
Deleting a VRF also removes those fallback entries. Table IDs cannot be
changed in place. Namespace startup replays configured VRFs, routes and rules.

## Physical and kernel evidence

* `STATIC-ROUTING-20260910.jsonl`: 1,200 intact physical forwards and720
  expected blocks, including IPv4/IPv6 next-hop forwarding beyond connected
  prefixes and blackhole route enforcement.
* `VRF-ROUTING-20260910.jsonl`: after booting the router kernel, 1,200 intact
  physical forwards and720 expected blocks, including forwarding inside a
  VRF and60 cross-VRF IPv6 isolation probes. Original settings restored.
* `ROUTER-CONTROLS-20260910.jsonl`: disposable DP namespace checks for two
  VRFs with overlapping IPv4/IPv6 addresses, weighted ECMP installation,
  source-policy route selection/removal and complete VRF cleanup.
* 18 network/overlay validation and rollback tests pass on the VM.
* ARP/NDP and20 IPv4 plus20 IPv6 echo replies passed on the physical fabric
  after the router-kernel boot (`ROUTER-HOST-CONNECTIVITY-20260910.jsonl`).

The kernel/configuration/AES dependency correction and rollback paths are
documented in `VIRTUAL-NETWORK.md`. Test VRFs/routes/policies were removed;
the user's original port settings remain active at revision59. This is not
full-appliance cold-boot qualification with a populated routing configuration.

## Dynamic routing runtime

FRR10.7.1 is built and installed on DP under `/usr/local/ffn-router`.
The build follows FRR's [cross-compilation workflow](https://docs.frrouting.org/projects/dev-guide/en/latest/cross-compiling.html),
using host-native clippy and MIPS64-BE libraries: libyang3.13.6 (with
ENABLE_LYD_PRIV), json-c0.19 and c-ares1.34.8. All37 packaged ELF files were
checked for ELF64, big-endian, MIPS before staging. Runtime archive SHA256:
`060a077c444789beccd2514c023fb6a6aea0e2e465449ecbfeafaf26be5f4ddc`.

`test-frr-routing.py` starts two temporary routers in isolated DP network
namespaces. BGP learned IPv4/IPv6 loopback routes in both directions; then
OSPFv2/OSPFv3 learned them after BGP was stopped. Each phase passed12 ICMP
echo requests using the advertised loopback source addresses. Kernel route
protocol attribution was checked before sending traffic. Daemons, sockets
and namespaces are isolated from `ffn-data`; TCP VTY listeners are disabled.
All lab processes/namespaces are removed in cleanup. Evidence:
`FRR-ROUTING-20260910.jsonl`; detailed daemon logs remain on DP.

No persistent FRR service, physical BGP peer, or OSPF interface has been
enabled. Deployment still needs the user's VRF names, port assignments,
BGP ASNs/peer addresses and OSPF areas, plus persistent daemon supervision
and configuration management. Dynamic routing within Linux VRFs and on
physical peers still needs qualification; the FRR test uses separate
namespaces. The commissioning build disables capability-based privilege
separation and runs its isolated lab daemons as root. SNMP, RPKI, protobuf
and several optional integrations are disabled; they are not claimed tested.

## Rebuilding FRR on the VM

Use `/mnt/clones/debian-mips64/sid-host`, with working mips64-linux-gnuabi64
cross tools and target PCRE2/readline development libraries. Host clippy
requires native libelf-dev and python3-dev. In `/build/ffn-router`, fetch
the pinned Debian source versions listed in `frr-sources.sha256` using
`apt-get source --download-only`, verify the manifest with `sha256sum -c`,
and extract each `.dsc` with `dpkg-source -x`. This also checks the Debian
patch archive against its descriptor. Run `build-frr-deps.sh`, then
`build-frr.sh` inside the chroot. The install tree is
`/build/ffn-router/frr-install`; dependency libraries are in
`/usr/local/ffn-router/lib`. Runtime tests set LD_LIBRARY_PATH to that prefix.
No owner SDK binaries or private credentials are included in these sources.
