# Optional PA-5200 VIF integration

The selected PA-5200 extension provides MP controls for logical interfaces on
the DP. `ffn_vif.py` validates assignments and adds/removes a single 802.1Q wire
tag. `ffn_vif_runtime.py` owns Linux TAP devices in `ffn-data` and connects them
to the existing PKI/SSO/PKO3 `ffnpkt0` transport. No legacy binary module is loaded.

## Reference material

Inspected on the VM:

- `/mnt/clones/ffn-vendor-drivers/mp/modules/if_vif/if_vif.ko`
- `/mnt/clones/ffn-vendor-drivers/mp/modules/fabric_vif/fabric_vif.ko`
- `/mnt/clones/5220-sysroot1-full/opt/pan/device/network/config/vif/pan_vifconfig.sh`
- `/mnt/clones/5220-sysroot1-full/var/cache/pan/device/network/config/vif/vif_install.sh`
- The adjacent `vif_rt_tables` routing-table definitions.

The vendor modules expose netdevices through private PCI and ixgbe/yxg packet
hooks; their Linux 3.10 ABI cannot be used on FFN's current kernel. The scripts
assign VLAN uppers and per-router routing tables. FFN uses supported TAP, bridge
and VRF interfaces with explicit source-port metadata and revisioned ownership.
This implements DP logical interfaces, not the vendor MP-host `fvif0` ABI or an
MP-host packet tunnel. FE100 hardware VIF session offload remains disabled.

## Controls

The WebUI page is **Network → Virtual Interfaces**. It appears only for the
selected PA-5200 extension. Loading the asset does not probe hardware.

- Create/edit/remove a VIF and assign its front port and wire VLAN (or untagged).
- Enable/disable it and choose L2 bridge membership or L3 addresses/VRF.
- Start/stop the packet transport and recover an interrupted DP assignment.
- Inspect revision, runtime state, drops and per-VIF RX/TX counters.

Authenticated GET `/api/system/runtime/vifs` returns state. Administrator-only
POSTs `/api/system/runtime/vifs/{set,start,stop,recover}` execute through the MP
control daemon with audit records and its durable operation journal. Equivalent
routes exist under `/api/pa5200`. A `set` replaces the complete VIF dictionary;
the revision must match observed state. Other operations take only `revision`.

Example shape (not installed automatically):

```json
{"revision":0,"vifs":{"fv9":{"port":5,"vlan":200,"enabled":true,
 "network":{"mode":"l3","addresses":["198.18.0.1/24"],"vrf":"vrf-lab"}}}}
```

Create `vrf-lab` through the existing network controls first. For L2 use, e.g.,
`{"mode":"l2","vlans":[100],"pvid":100}`. That bridge VLAN is independent
of the wire VLAN and allows VLAN translation between logical interfaces.
The existing bridge uses STP, so L2 forwarding may wait for bridge convergence.
Connected L3 routes are installed by Linux. The Routing page also accepts VIF
names in static-route `dev`, ECMP nexthop `dev` and policy-rule `iif` fields.
See `VIF-ROUTING-20260915.md` for dependencies and physical qualification.

Configuration persists in `/etc/ffn/vifs.json`; a durable pending record enables
rollback after interruption. `/run/ffn-vif.sock` is root-only. The fabric lock
excludes other TAP/physical consumers. Set/recover operations also take the
network lock. Before MP mutations, the existing CP policy barrier drains old
FE100 sessions and leaves admission blocked. Direct DP CLI access is an
administrator interface; use MP controls for the complete policy barrier.

Reassignment closes old TAP handles, updates only owned devices, drains queued
frames and publishes the new mapping. Unknown/disabled selectors, oversized
frames, nested tags and tagged logical TX are dropped. Queue pressure is
counted without unbounded retries. Periodic device checks stop delivery if
ownership, addresses, MTU, admin state, bridge membership or VRF attachment
drifts. Network edits cannot delete referenced VRFs, reuse VIF addresses or
stop the namespace while assignments remain. Reassignment refuses unmanaged
static/dynamic routes on affected VIFs.

The service defaults to commissioned ports **5 and 13**. The schema accepts
front port numbers 1–24, but uncommissioned assignments are rejected by the
runtime. Other ports require their physical trunk/return paths to be qualified
before the service port allowlist is expanded. MTU is at most 1500. This is a
copying userspace transport, not line-rate or production firewall qualification.
`forwarding` means attached runtime handles, not proof of physical carrier or
end-to-end delivery. Use Faceplate Ports for physical state.

## Installation and validation

`management/install-vif.py` installs staged modules into the explicitly selected
MP extension and DP NFS root. It backs up changed files, preserves newer BCM/PHY
controls and patches only VIF integration into the installed network controller.
Reload DP systemd and restart the MP daemon/API after installation. It does not
start forwarding or enable the service at boot; no ports are assigned by default.

Tests: `test_vif.py`, `management/test_vif_backend.py`,
`management/test_vif_api.py`, `management/test_vif_ui.cjs`, and root-only
`test_vif_kernel.py` in a disposable network namespace. The kernel test checks
L2 packet delivery, VRF IPv4 forwarding, TTL/checksum/MAC changes and drift
rejection. `management/validate-vifs-live.py` exercises the MP daemon and the
5–13 physical DAC, temporarily enabling that pair at 10 Gb/s and restoring saved
port settings and empty VIF assignments afterward.

Initial physical testing found both saved ports disabled; its zero-return result
is retained in `VIF-PORTS-DISABLED-20260915.json`. See the final validation JSON
for successful paths and cleanup evidence, rather than inferring delivery from
API acceptance or TX counters alone.

The VLAN run (`VIF-VLAN-VALIDATION-20260915.json`) verified 4/4 exact frames in
each direction, 0/4 delivery after changing only the receive selector, and 4/4
again after updating the sender to the same VLAN. The disabled-interface phase
initially stopped the test harness on the expected native `ENETDOWN` error;
assignment cleanup and saved front-port restoration still completed. The
corrected probe observes both the physical return and the logical receiver so
zero logical delivery cannot be mistaken for an unplugged cable.

The real-kernel bridge and VRF tests passed on both the build VM and OCTEON DP:
L2 delivery, IPv4 routed delivery with TTL 63 and valid header checksum, and
rejection of altered bridge VLAN membership and VRF attachment. These tests use
a disposable namespace and do not claim external IPv6 or line-rate forwarding.

Final disable/re-enable results are in `VIF-DISABLE-VALIDATION-20260915.json`:
all four disabled-VIF test frames were observed returning from the physical
DAC, with zero logical deliveries; re-enabling restored 4/4 physical and logical
delivery. Both saved ports were restored to disabled/auto. Cleanup left VIF
revision 11 with an empty assignment dictionary, no owned VIF netdevices, no
pending recovery and an inactive VIF service. The existing network configuration
remains at revision 60. `VIF-CLEANUP-20260915.json` records zero rejected TX and
zero bad DMA, with all 318 accepted TX completed at that snapshot.

`VIF-INSTALL-VERIFIED-20260915.json` verifies the selected extension exposes both
API prefixes and its WebUI page, while a generic platform has no VIF routes.
The MP API and control daemon are active. The latest installation backup is
`/var/backups/ffn/vifs-1789490684415516429` on the MP.
