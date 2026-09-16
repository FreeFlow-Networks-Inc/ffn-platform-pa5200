# Copper networking through the MP, CP and DP

The commissioned copper 3/4 path uses BCM14/15, the BCM24 TM_SSP return
trunk, and the OCTEON Linux VIF owner. The VM sysroot distinguishes this
packet transport from the FE100 session engine. The latter requires its own
flow-add/update/delete lifecycle; an XML configuration or a linked PHY does
not establish session offload.

Reference files under the VM sysroot include
`opt/dpfs/usr/share/pdt/fe100.py`,
`opt/dpfs/usr/lib/python2.7/site-packages/pcs/packets/fe100.py`, and
`opt/dpfs/usr/local/lib64/libpandp_cp.so.1.0`. Read the diagnostic Python as
text; importing it performs hardware initialization. No vendor binary or
diagnostic source is included in this integration.

`ffn_copper_forwarding.py` runs on CP and uses the existing BCM daemon.
It never attaches another SDK, initializes the switch, allocates queues or
changes PHY registers. Its scope is exactly copper3=BCM14 and copper4=BCM15.
WAN1 and unused copper2 are outside this controller's scope.

The MP `vifs` resource drains the existing policy admission path, enables the
CP return path, then starts or changes DP assignments. Stop detaches the DP
before disabling the CP returns. Recovery can clear an interrupted CP change
only after the DP operation succeeds. WebUI and CLI use this same resource.
Existing VIF configuration supports wire VLAN selectors, L2 bridge VLANs,
L3 addresses and virtual routers; the network resource supplies static routes
and policy-routing rules.

## Readiness and recovery

CP state is stored in `/etc/ffn/copper-forwarding.json`. Intent is fsynced
before either hardware write. A failed operation stays pending and cannot
advertise readiness. `status`, `start`, `stop`, and `recover` are the only
accepted controller operations. Recovery disables the owned pair and verifies
the result. Existing redirects to another destination are never overwritten.

`/etc/ffn/copper-forwarding-qualified.json` is an operator commissioning
record with schema `1`, ports `[3,4]`, and an `epoch` matching the controller's
status. Create it only after a successful physical packet test with unchanged
CP boot ID and BCM daemon PID/start time. The controller cannot self-certify
from link or queue inventory. Qualification is invalid after a CP reboot or
BCM owner restart. Cold-boot queue provisioning is still a separate task.

Even with a qualification record, readiness requires TM_SSP header type 11,
exactly eight queues on each of BCM14/15/24, and both ingress redirects to
BCM24. Every observation reads the hardware through the existing owner.
The DP additionally requires its commissioned wiring profile, matching PHY
and MAC speeds, administrative enablement, and fresh link observations.

The observation deadline is 30 seconds from DP challenge issuance. CP
inventory takes approximately 6â€“8 seconds on this appliance; the old
12-second deadline could expire between otherwise successful poll cycles.
The new deadline covers two normal cycles. Explicit link loss still lowers
carrier on the next observation. Missing communication expires the lease;
late, replayed and out-of-order responses cannot extend it. The core plane
daemon permits status observations during changes to other resources. Reads
and writes to the same resource remain serialized because its backend may
require an exclusive lock. Configuration writes retain global serialization.

Install with `install-copper-vif.py`, staging the new CP controller alongside
the documented MP/DP files. Installation does not create a qualification
record, start transport or certify any new ports. Existing installations can
upgrade with stopped, empty VIFs and backups. No reboot is required.

## Physical validation

First run the bounded raw test in `bcm/COPPER-FORWARDING.md`. Then install
`validate_vif_forwarding.py` and `validate_trunk_io.py` in DP
`/usr/local/sbin`, and run the MP harness with the manager's Python environment:

```sh
python3 validate-vif-forwarding-live.py --copper
python3 validate-vif-forwarding-live.py --copper --neighbors
```

The harness requires an empty stopped VIF runtime, the physical copper3/4
cable, and free reserved test VLANs/VRF. It uses distinct ingress and return
wire VLANs so the cable does not recirculate test packets. Tests cover bridge
forwarding, VLAN isolation, IPv4 TTL/checksum, IPv6 hop limit, static routes,
policy routing, and actual ARP/ND exchange. Configuration goes through the MP
daemon and is restored afterward. Raw packet success is not a throughput or
FE100 security/session-offload certification.

Network validation now runs the DP attachment and dependency checks before
creating an apply intent. A request for an unattached physical port is rejected
without leaving a transaction with an unknown outcome. This does not attach
or enable a packet backend automatically.

## PA-5220 verification, 2026-09-16

The physical copper3/4 cable passed 32/32 tagged 1518-byte raw packets, eight
exact bridged frames, eight correctly isolated frames with zero leakage,
eight IPv4 packets with TTL/checksum updates, and eight IPv6 packets with
hop-limit updates. Both directions were tested in every forwarding case.
Static routes and policy rules were installed through the MP resource in a
temporary virtual router. Removing an assignment with dependent routes was
rejected. The harness restored the network, removed its VIFs and stopped the
transport after the test.

A second run with fresh interfaces passed another eight IPv4 and eight IPv6
packets after exchanging real ARP and ND requests/replies over the cable.
All four next-hop neighbor entries reached `REACHABLE`. The authenticated
WebUI runtime API reported both copper VIFs ready at 10000 Mbps with active
packet forwarding and no runtime or recovery error during this run.

Two isolated tests on the actual OCTEON kernel also passed: TAP carrier
attach/close behavior, PHY/MAC transitions, the driver handshake, replay
rejection and unrefreshed lease expiry. Neither sent physical packets.

The software regression suites passed 37 tests across the core plane daemon,
MP adapters, CP controller and DP packet/carrier modules. Publication checks
passed for the changed platform files and the core repository.
