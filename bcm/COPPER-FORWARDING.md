# Copper packet forwarding: sysroot evidence and live validation

Validated on a PA-5220 on 2026-09-16. The physical RJ45 cable joins panel
ports **3 and 4**. Port **1 is WAN** and was not reconfigured.

## What the sysroot establishes

The owner's `/opt/dpfs` contains these references; no vendor binaries or
source files are copied into this repository:

| Reference relative to sysroot | Evidence |
| --- | --- |
| `usr/share/broadcom/config.bcm`, lines 108, 413, 432–480 | PP mode, DSA support, per-port ingress/egress header types |
| `usr/share/broadcom/dsa_tag_support.c` | Ingress PMF direct extraction of destination, traffic class and drop precedence from DSA fields |
| `usr/lib/python2.7/site-packages/pcs/packets/dune.py`, `itmhv3` | Jericho incoming traffic-manager destination encoding |
| `usr/lib/python2.7/site-packages/pcs/packets/fe100.py` | FE100 packet, SYSPORT, exception, session and flow messages are distinct types |
| `usr/local/lib64/libpanbcm_cp.so.1.0`, `bcm_copper_phy_initialize` | Board-specific copper PHY initialization; see [link control](../management/LINK-CONTROL.md) |

The sysroot and live SDK mapping agree on the copper MAC/TM ports. Panel
numbering also uses the operator-confirmed wiring profile:

| Panel | PHY address | BCM port / TM port | BCM core | Observed link |
| --- | --- | --- | --- | --- |
| 1, WAN | 17 | 28 | 1 | 1 Gbps |
| 2 | 16 | 13 | 1 | No peer |
| 3 | 19 | 14 | 1 | 10 Gbps |
| 4 | 18 | 15 | 1 | 10 Gbps |

Copper ingress is `RAW`; egress is `RAW_DSA`. BCM24 is the internal
OCTEON link, core 0 / TM24. The sysroot's original BCM24 settings are
ingress `TM`, egress `RAW`. The current FFN commissioning configuration
uses **TM_SSP (SDK value 11)** to retain source-port metadata on return.
Do not assume the stock configuration and current FFN configuration match.

## The path actually demonstrated

```text
OCTEON ffnpkt0 / PKO
  -> BCM24 injected traffic-manager header
  -> destination system-port VOQ -> connector -> MAC14 -> PHY19 -> RJ45 3
  -> external cable
  -> RJ45 4 -> PHY18 -> MAC15 -> ingress destination BCM24
  -> OTMH with source system port -> OCTEON PKI / ffnpkt0
```

The reverse direction exchanges MAC14/PHY19 and MAC15/PHY18. The MP and CP
configure this path; packets are generated and received locally on OCTEON.
Neither SSH nor the MP carries the test frames.

The working FFN injection format is four bytes `01 <BCM destination BE16> 00`,
then Ethernet DA/SA, eight DSA placeholder bytes, then the original EtherType
and payload. The tested return is `00 18 <source BCM port BE16>` followed by
the intact Ethernet frame. Return source 14 maps to panel 3; source 15 maps
to panel 4. RX and TX envelopes differ. Link FCS is absent from the returned
Ethernet payload. These observations apply to the commissioned TC0 direct
path; they are not a general decoder for every switch or FE100 header.

## Why the copper loop had link but no usable packet path

Before this investigation, BCM14 already had an eight-queue VOQ bundle;
**BCM15 had none**. Both copper ingress force-forward destinations were
disabled. A synchronized PHY/MAC and an enabled port cannot substitute for
queue resources, scheduler connections and an ingress destination.

The bounded test added a BCM15 connector with all eight queues attached to
the port scheduler, an eight-queue VOQ with the existing 10G slow-enabled
credit profile, and both ingress/egress queue connections. The allocation
returned connector `0xc4080038` and VOQ `0x243c004c`; traversal subsequently
reported VOQ `0x2400004c`. These are observed allocation handles, not IDs to
hard-code or reuse after a switch restart. Physical MODPORT was `0x0800080f`
and system-port GPORT was `0x6c00000f`.

Temporarily directing BCM14 and BCM15 ingress to BCM24 then allowed both
directions to return to OCTEON. No direct front-port bridge was installed.

| Ethernet size, excluding FCS | VLAN | Frames sent | Exact returns | PKO completions / wire TX / wire RX |
| --- | --- | --- | --- | --- |
| 64 bytes | Untagged | 8 | 8 | 8 / 8 / 8 |
| 1,514 bytes | Untagged | 32 | 32 | 32 / 32 / 32 |
| 1,518 bytes | 802.1Q VID 123 | 32 | 32 | 32 / 32 / 32 |

Each run included both directions, checked unique nonce/sequence and exact
frame contents, required the expected physical return port, and observed no
DMA or trunk errors. This proves bounded packet transport and tag preservation,
not line-rate throughput, routed connectivity, NAT, security policy, or session
offload. No traffic test was performed on WAN port 1 or unused port 2.

## Reproducing the bounded test

Use the existing BCM owner daemon's allowlisted `cint.run` operation and
fixed `ffn_bcm_forward_test.c` staging path. Do not open a second SDK process.
The following modes are rendered by `prepare-forward-test.py`:

- `tm-front4-allocate` (64): allocate only BCM15 after confirming no existing
  BCM15 VOQ and the expected core/TM mapping. This is a manual commissioning
  operation, **not an idempotent provisioning loop**. On a partial failure,
  inspect connectors, attachments and queues before cleanup; never retry
  blindly. It does not automatically unwind a partial allocation.
- `copper-return-pair-enable` (65): require TM_SSP on BCM24 and eight-queue
  bundles for BCM14, BCM15 and BCM24. Reject an existing ingress destination
  other than BCM24. Enable and read back both returns; attempt restoration of
  the prior enable states if a write/readback fails, reporting rollback errors.
  Queue presence alone is not a connectivity proof; run the packet test.
- `copper-return-pair-disable` (66): disable only these two return paths and
  verify readback. Requires the same known destination ownership. WAN is not
  included in either return mode.

On the DP, with the VIF packet owner stopped and the explicit wiring profile:

```sh
PYTHONPATH=/usr/local/sbin python3 validate_trunk_io.py \
  --ports 3,4 --pairs 3:4,4:3 --rx-format bcm-otmh-ssp \
  --copper-profile /etc/ffn/vif-copper.json \
  --count 16 --size 1518 --vlan 123 --seconds 3
```

The validator holds the same exclusive fabric lock as the runtime, does not
change wiring or certification, and only records raw samples containing its
own nonce. An explicit copper profile is required; there is no guessed copper
mapping. Software regression tests cover mapping rejection, qualification
preservation, tagged frame construction and source-port decoding.

## State left behind and next integration requirements

Both temporary copper ingress return settings were **disabled and read back**
after testing. The added BCM15 queue remains allocated but its test return is
inactive. Existing WAN and optical return settings were preserved. Copper
`packet_path_verified` flags remain false and the VIF service remains stopped
with no assignments. No reboot or BCM restart was performed or required.

Persistent provisioning still needs the MP-owned control path to reconcile
queue/connector ownership, headers and ingress destinations through the CP
BCM daemon, with recovery from partial application and switch restarts.
Readiness must invalidate when that hardware state disappears; a saved PHY
mapping or a successful probe from a previous switch lifetime is insufficient.
Only then should the DP VIF/configuration owner activate a port and validate
ARP, DHCP, VLANs, L2/L3 forwarding and policy on the actual wire.

FE100 uses separate BCM3/BCM20 links and its own control protocol. Sysroot
`fe100.py` distinguishes SYSPORT packets from session-bind, flow-add/update,
exceptions and control messages. The direct BCM24 test bypasses that session
engine. FE100 offload requires separate programmed-state, hit-counter and
packet-behavior evidence; `session_offload_verified` remains false.
