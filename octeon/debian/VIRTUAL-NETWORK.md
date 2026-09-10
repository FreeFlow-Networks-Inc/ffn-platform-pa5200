# Virtual networking on the MIPS64-BE DP

The PA-5220 DP now runs `6.18.49-ffn-debian-dp-router+`, built by
`build-virtual-network-kernel.sh` with `VARIANT=router` and the prior virtual
tree as `SOURCE_TREE`. Its complete configuration is `config-dp-router`.
Image SHA-256: `757a2141e40bf21bafacadbf2a132a9141cc272cd40705390297317e7f86b0b5`.
It booted successfully on the physical OCTEON DP on 2026-09-10.

The previous virtual kernel lacked VRF support: the script incorrectly
requested `VRF` instead of `NET_VRF` and omitted `NET_L3_MASTER_DEV`.
The script now enables and checks both required symbols. The earlier claim
that VRF was available in the virtual kernel was incorrect.

Modules enable MACsec, VXLAN, Geneve, GRE/GRETAP, IPIP, OVS, TC flower/actions,
VRF, bonding and XFRM/IPsec. Not every enabled module is packet-tested.
`test-virtual-network.py` tested the first six features in disposable DP
namespaces: 100 packets each direction, 1400-byte inner IP for the tunnel
tests. MACsec additionally captured encrypted Ethernet frames, found no
chosen plaintext marker and rejected traffic after substituting a wrong key.
These are namespace tests, not physical SFP/QSFP tunnel interoperability tests.

MACsec window0 intermittently lost a packet even with generic AES; window64
passed. Reordering is a possible explanation, not a proven root cause.
The controller defaults to a 64-packet replay window and permits explicit
configuration. It does not implement MKA or accept/persist association keys.

## Runtime management from MP

`ffn-overlay status` returns configuration, revision and live interfaces.
Pipe a complete `{ "revision": N, "links": {...} }` object to
`ffn-overlay set`; use the current revision. Managed interface names start
with `ov`; kinds are `vxlan`, `geneve`, `gretap`, `gre`, `ipip`, `macsec`.
Underlays must be configured L3 ports. Tunnel local endpoints must already be
assigned to them. VXLAN/Geneve take a `vni`, GRE optionally takes a numeric
`key`; all take an encapsulation-safe `mtu` and optional `addresses` list.
Geneve checks the route's selected source and interface before creation.

The MP controller created, inspected and deleted each kind on physical DP
underlay p5. The final persisted overlay map is empty. Ownership aliases,
revision checks, rollback and a shared network lock protect updates. Native
port reconfiguration rejects changes to underlays used by a managed overlay;
delete the dependent overlays first. `ffn-overlay.service` is enabled for
replay after `ffn-network.service` at boot. Its live start passed; replay after
a subsequent reboot remains untested.

## OVS and OVN

`build-sdn.sh` reproduces the cross-build of OVS 3.7.1 and OVN 26.03.2 with
hash-checked official source archives. Runtime binaries and schemas are
installed in DP `/usr/local`. This commissioning build disables SSL and uses
private UNIX sockets with SSH management; it must not expose unencrypted
remote database listeners. No permanent OVS/OVN takeover of native ports is
configured.

`test-sdn-fabric.py` on the MP runs private OVS and OVN databases and restores
native configuration in finally. Both OVS and a single-chassis OVN logical
switch passed 300/300 packets with no loss, duplication or corruption over:
front3 -> cable -> front1 -> FE100 -> MP relay -> DP -> front5 -> cable -> front13.
The OVS rule counter also reported 300 packets. This is kernel software
forwarding. `tc ... skip_sw` failed as unsupported on the TAP; no hardware
offload is claimed. OVN port binding was verified on one chassis; interchassis
Geneve transport was not exercised. See [OVN architecture](https://www.ovn.org/support/dist-docs/ovn-architecture.7.html).

Evidence is in `VIRTUAL-NETWORK-VALIDATION-20260909.jsonl` and
`SDN-FABRIC-VALIDATION-20260909.jsonl`. SDK acceleration and remaining physical
offload work are documented in `SDK-OFFLOAD.md`.

## Kernel rollback

The CP's original DP kernel and `network.conf` override are preserved.
The new override is `/etc/systemd/system/ffn-dp-boot.service.d/virtual.conf`
on CP. To roll back, remove only that override, daemon-reload, stop the MP
fabric, restart the guarded CP DP-boot service, wait for DP network readiness
and restart the MP fabric. Rebuild/remove the kernel-specific AES module as
appropriate. This procedure affects DP; BCM and CP need not be restarted.

The router successor uses `zz-router.conf` in the same override directory.
To revert specifically to the virtual kernel, remove only `zz-router.conf`,
restore `/usr/local/lib/ffn_octeon_aes.virtual.ko` as
`/usr/local/lib/ffn_octeon_aes.ko` on DP, then use the guarded DP restart
procedure above. Do not leave VRF-dependent configuration when reverting
to a kernel without VRF support.

The router AES module SHA-256 is
`6cea081e7b21e14aa20f049649f5bd550ac3a3c78886b6739e77c259cfbcae73`.
A clean boot exposed an existing missing dependency in the AES service:
bare `insmod` did not load `crypto_algapi`. The startup script now loads
that dependency with `modprobe` first. The service and registered AES driver
were verified active after that correction.
