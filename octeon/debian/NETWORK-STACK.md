# Runtime L2/L3 control from the MP

The DP software stack uses a dedicated `ffn-data` network namespace. The
management interfaces and routes stay in the original namespace. Logical
ports p1..p24 are TAP devices. The commissioning relay now connects physical
ports 1, 3, 5 and 13 through BCM/FE100 to the DP. Backend status reports whether
the relay is attached; other TAP names do not imply a physical connection.
See `FORWARDING-FABRIC.md` for hardware tests and startup limitations.

The kernel includes bridging with VLAN filtering, 802.1Q, veth, IPv4/IPv6
routing and netfilter/nftables support. The initial service config is an
isolated lab setup, not a production front-port configuration.

## MP interface

`ffn-network status` returns JSON with the current revision, per-port config,
live addresses/routes and backend status. `ffn-network patch` reads JSON on
stdin and applies only changed ports. The existing pinned MP→CP→DP SSH path
provides authentication; no unauthenticated listener is added.

Example: obtain the current revision, then submit a change using that value:

```sh
ffn-network status
ffn-network patch <<'JSON'
{"revision":0,"ports":{
  "p1":{"mode":"l2","vlans":[100],"pvid":100},
  "p3":{"mode":"l2","vlans":[100,200]},
  "p5":{"mode":"l3","addresses":["198.18.1.1/24","fd52:20:1::1/64"],"mtu":1500},
  "p13":{"mode":"disabled"}
}}
JSON
```

Each supplied port object completely replaces that port's settings. Other
ports remain unchanged. An L2 port with a pvid accepts untagged input into
that VLAN and sends that VLAN untagged. Allowed VLANs without a pvid are
tagged-only. L3 ports have connected routes for configured IPv4/IPv6 prefixes.
Static routes, SVIs and routing protocols are not exposed by this controller.

The revision must match the current config; stale updates fail. Validation
precedes network changes, and a file lock serializes callers. Changed ports
are briefly taken down while their membership/addresses change. STP applies
its normal convergence delay for L2 ports. This is runtime reconfiguration,
not a claim of lossless transitions. Unchanged ports are not recreated.

The config is saved by atomic rename to `/etc/ffn/network.json` on the DP.
Failure restores previously applied port settings; rollback errors are
reported explicitly. The `ffn-network.service` recreates this state at boot.
Restoring settings does not restore learned MAC entries or dynamic neighbors.

## Development and verification

`build-network-kernel.sh` builds an isolated copy of the existing DP kernel
on the VM. The CP boot helper accepts an `FFN_DP_KERNEL` override, retaining
the previous image as its default fallback. The network image is selected
by the CP `ffn-dp-boot.service.d/network.conf` drop-in. Restart only through
the guarded DP boot service, which stops the CP-side transport writer first.

`test_network_config.py` checks validation, revision conflicts, changes to
only the selected port and rollback after a persistence failure.
`test-network-stack.py` injects packets through TAP on the real DP to test
L2 forwarding/learning, VLAN isolation, IPv4/IPv6 routing and L2→L3 changes.
It requires the supplied lab config and restores changed port settings.
These tests do not exercise the physical BCM/FE100 front-port backend.

Hardware validation on 9 September 2026 passed 100 L2 packets, MAC learning,
100 IPv4 packets, 100 IPv6 packets, VLAN isolation after STP convergence,
100 IPv4 packets after changing the L2 ports to L3, and another 10 on the
unchanged routed pair. IPv4 checks include destination/source MAC rewrite,
TTL decrement, IP checksum and UDP payload integrity. IPv6 checks include
hop-limit decrement and payload integrity. A separate ARP request received
the configured gateway reply. See [packet results](NETWORK-VALIDATION-20260909.txt)
and [ARP result](NETWORK-ARP-VALIDATION-20260909.txt).
The [MP command test](NETWORK-MP-VALIDATION-20260909.txt) changed p5's MTU
at runtime, rejected a stale revision, preserved p13, and restored p5's
original settings. No processor or service restart was needed for the update.

Running DP release: `6.18.49-ffn-debian-dp-network+`. Kernel SHA-256:
`c72c68ba20aae6574b9926fe7e41aad12cbe9593f3aac9aaa7890891e6bc6b95`.
The original kernel remains available for rollback. The new network service
is enabled with the isolated lab configuration; no faceplate links are moved
into the namespace by this installation.

The VM owner FE100 diagnostic defines LIF forwarding types 2=L2, 3=L3 and
5=SYSPORT. The preceding FE100 test only established SYSPORT forwarding.
Linux route/bridge configuration does not program those FE100 offload tables.
The four-port packet backend is now attached; programming hardware L2/L3
offload remains separate work. See `../../fe100/DRIVER-REFERENCE-20260909.md`
for the inspected ABI and `FORWARDING-FABRIC.md` for physical validation.
