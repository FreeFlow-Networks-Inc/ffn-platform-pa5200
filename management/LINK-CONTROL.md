# Link, PHY and switch service control

All WebUI and CLI requests pass through authenticated MP API routes and the MP
control daemon. The CP exposes fixed operations, not arbitrary registers or shell
commands. Each change requires a current revision; ambiguous outcomes remain
journaled and block further writes.

Network > Faceplate Ports offers supported SDK speeds and an immediate Apply
speed action. Network > Interfaces > Advanced uses the selected platform's
capabilities and stores link speed in candidate XML for configd to apply on
commit. Fixed SDK speed disables auto-negotiation; Auto preserves the existing
advertisement. SDK ability bits are intersected with the board port class.

CLI examples (immediate persistent platform operations):

```
show platform faceplate
request platform interface ethernet1/5 link-speed 1000
request platform interface ethernet1/5 link-speed auto
show platform phy
show platform bcm
request platform bcm restart acknowledge-link-outage
```

BCM start/stop/restart explicitly acknowledges a link outage. Requests return
pending, not forwarding success. Refresh service health, recommit configuration
and verify forwarding after startup. Updating the BCM Python handler on disk
requires a BCM service restart before its new operations are available; this can
reinitialize the ASIC and interrupt every faceplate link. No automatic restart
is part of deployment.

Copper PHY support uses the CP's locked SMI bus 0 and verifies identity
600d:84f9 and running firmware. Advertisement can be constrained to 100M, 1G or
10G while retaining auto-negotiation. Pause and unrelated register bits are
preserved; half-duplex advertisement is removed. The kernel write gate is restored
on success and failure. No firmware image or flash write is introduced.
`phy/set` accepts revision, phy (16..19), speed (auto/100/1000/10000). The UI
shows PHY addresses separately from faceplate port numbers. The corrected mapping is
port 1/PHY17, port 2/PHY16, port 3/PHY19 and port 4/PHY18. PHY settings alone do not prove WAN forwarding. Copper control requires a
commissioned PHY-to-MAC association; the rate follower handles MAC synchronization.

Install the CP PHY helper beside ffn_mdio.py and the copper service drop-in to
restore saved advertisements after firmware initialization. Local recovery after
inspecting an uncertain PHY operation is `ffn_phy_control.py resolve`; this
explicitly accepts observed advertisement state. Faceplate state has its separate
existing resolve command and boot restore unit.

SDK reference: https://github.com/Broadcom-Network-Switching-Software/OpenBCM/blob/master/sdk-6.5.16/include/bcm/port.h
PHY register reference: https://github.com/Broadcom-Network-Switching-Software/OpenBCM/blob/master/sdk-6.5.16/src/soc/phy/phy8481.h

## Sysroot negotiation integration

The owner's sysroot `usr/local/lib64/libpanbcm_cp.so.1.0` was inspected
read-only. `_phy_8481_copper_an_set` at `0x12736410` enables/restarts both
the legacy MII control (`7.0xffe0`) and 10G AN control (`7.0x0000`), in that
order, using mask `0x1200`. The same sequence is documented in Broadcom's
[PHY8481 driver](https://github.com/Broadcom-Network-Switching-Software/OpenBCM/blob/master/sdk-6.5.16/src/soc/phy/phy8481.c).
Our former speed controller only restarted the second register. The CP driver
now follows both steps, preserving unrelated bits and checking the enable bit
after each write. The restart bit may self-clear and is excluded from revisions.
No vendor binary is distributed or executed by this integration.

The MP-owned `faceplate` resource accepts a standalone request:

```json
{"revision":123,"port":3,"restart_autoneg":true}
```

Use the current faceplate revision. The operation requires a mapped, enabled
copper port, running firmware, valid register observations and no pending
configuration. It changes only the selected PHY's negotiation controls, retains
its advertisement, and never resets the chip, reloads firmware or starts packet
forwarding. A partial failure leaves the existing PHY/faceplate journals pending.
Restart actions are not persisted as boot intent.

Network > Faceplate Ports exposes **Renegotiate** and negotiation status.
The API returns separate legacy MII and 10G AN control/status observations. The privileged MP CLI uses the same daemon:

```sh
ffn-copper-port status --port 3
ffn-copper-port renegotiate --port 3
ffn-copper-port renegotiate --port 4
```

Install `copper-port-cli.py` as `/usr/local/sbin/ffn-copper-port`. Update the
CP PHY and faceplate helpers, MP `daemon_backend.py`, `control.py` and the
Faceplate Ports UI together; preserve other installed UI extensions. Only the
API process needs reloading; BCM, the PHY firmware and the firewall need no
restart. The test cable connects physical ports **3 and 4**; port **1 is WAN**.
Control readback verifies the request, not cable continuity or packet delivery.


## BCM activation and restart recovery

The BCM daemon must be restarted once after upgrading its SDK link handlers.
A restart interrupts switch links and reinitializes the ASIC; it does not reboot
MP, CP or DP. `ffn-bcmd.service` also starts `ffn-front-ports.service` so starting
from a stopped or failed state initializes the faceplate. The DMA prerequisite
accepts the allocator confirmation from either dmesg or the current boot's kernel
journal; a command-line reservation token alone is never sufficient.

Link ability parsing accepts complete SDK output lines only. CINT echoes input
including printf literals for unsupported rates, which must not become advertised
capabilities. Copper MAC rates follow the external PHY through separate fixed
SDK operations. PHY negotiation alone does not prove WAN forwarding.

On hardware, management-daemon requests verified an unused SFP port at 1G and
10G with readback; existing linked SFP and QSFP ports recovered at 10G and 40G.
These checks establish link control, not L2/L3 forwarding or a DHCP lease.
Saved administrative configuration can differ from current runtime; inspect it
before replaying configuration following a BCM restart.

Copper initialization has an independent service lifecycle, ordered after front-port
startup at boot but not stopped or restarted with BCM. Recovering its service
skips firmware loading for running PHYs, skips enable/AN restart for already
enabled PHYs, and restores identical speed advertisements without register writes.
This preserves existing copper negotiation during service recovery.


## Unified copper faceplate controls

The existing MP `faceplate` resource now coordinates the BCM88375 MAC and the
BCM84848 external PHY for copper ports. WebUI and CLI use the same requests as
optical ports: `request platform interface ethernet1/2 link-speed auto` or
100, 1000, 10000. Copper speed selection sets full-duplex auto-negotiation
advertisement; it is not a forced MAC SerDes rate. Enable/disable coordinates
both devices (MAC off before PHY off; PHY on before MAC on). Readback must match
both before success. A partial result remains journaled and blocks further writes.
PHY firmware busy checks and the kernel write gate protect MDIO mutations.

Physical mapping is separate from the MDIO address. The vendor address array is
pair-swapped, so consecutive addresses alone are not evidence of panel numbering.
The operator corrected the WAN label from ethernet1/2 to ethernet1/1. Its live
path is PHY17 -> BCM28, linked at 1G after SGMII selection. This agrees with the
vendor panel map; the earlier apparent mismatch came from the physical label.
The complete PA-5220 board map is:

| Panel port | PHY | BCM MAC |
|---|---:|---:|
| ethernet1/1 (WAN) | 17 | 28 |
| ethernet1/2 | 16 | 13 |
| ethernet1/3 | 19 | 14 |
| ethernet1/4 | 18 | 15 |

Store measured associations in `/etc/ffn/copper-map.json`. Each physical port key
(1..4) requires a `phy` (16..19) and `bcm_port` (28,13,14,15). Both sets must be
unique. The WAN entry is `{"1":{"phy":17,"bcm_port":28}}`.
There is no global mapping default: appliance-specific evidence stays local.
Legacy integer PHY-only entries remain readable but cannot authorize MAC writes.
Unknown associations disable copper faceplate writes. Configuration revisions
include both PHY and MAC mapping so stale requests cannot hit a different socket.

All four port mappings are now installed on this appliance and covered by
controller tests. The authenticated MP API exposes admin and speed controls for
all four. Auto negotiation and enable were verified on ports 3 and 4; the WAN
on port1 remained at 1G. The reported 3-to-4 cable loop still had no PHY link
after negotiation; control readback does not certify an external connection.

The faceplate page reports copper wire speed/link separately from switch-side
link. A wire link alone does not establish PHY-to-MAC synchronization, a dataplane
attachment, forwarding, or DHCP service. The local 1G PHY and MAC links have
both been measured up; dataplane attachment and DHCP are still separate work.
The GPIO/PHY bring-up service retains independent lifecycle control; BCM process
restart must not reset copper firmware or negotiation.


## Copper MAC rate follower

Install `ffn_bcm_copper.py` beside `ffn_bcmd.py` in the BCM compatibility root and
activate the new handler once with a deliberate BCM restart. It exposes only
`port.copper.status` and `port.copper.sync` for the four copper-facing MACs.
100/1000 use SGMII; 10000 uses XFI. SDK mutation preserves administrative enable
state and checks interface, speed, duplex and AN readback. It does not touch the
external PHY advertisement or restart its auto-negotiation.

Install `management/ffn_copper_link.py` in CP `/usr/local/sbin/` and the
`octeon/debian/ffn-copper-link.{service,timer}` systemd units. Enable the timer
after verifying the mapping and first synchronization. It follows the PHY every
five seconds after front-port initialization, sharing the faceplate/PHY locks
with MP-driven requests. User configuration remains owned by the MP daemon; this
worker performs the resulting hardware link maintenance on CP. It never starts a
stopped BCM service, enables a disabled MAC, or changes an uncommissioned port.

Runtime status is `/run/ffn-copper-link.json`, exposed as `copper_sync` in the
faceplate API and WebUI. An ASIC change is journaled in
`/var/lib/ffn/copper-sync-pending.json` before mutation. After a lost response,
matching SDK readback completes the operation; a mismatch blocks further sync
and remains visible for inspection. The timer does not repeatedly mutate an
uncertain state.

Reference: Broadcom's `phy_8481_link_up` in
https://github.com/Broadcom-Network-Switching-Software/OpenBCM/blob/master/sdk-6.5.16/src/soc/phy/phy8481.c
selects SGMII and follows the negotiated rate for 100M/1G copper links.

## Identify all four copper panel ports

The MP resource `copper-identify` records physical panel associations through
the CP controller `ffn_copper_identify.py`. Install it with
`management/install-copper-identify.py`; the script stages its listed files,
preserves other MP commands and UI sections, and reloads the MP daemon and
manager API when its router changes.
It neither restarts BCM/PHY services nor changes a physical mapping on install.

Administrators use **Network → Faceplate Ports → Identify copper ports**:

1. Select an unmapped port with no cable attached and start identification.
2. Connect one spare active Ethernet peer to that exact port. Leave existing
   links, especially WAN port1, in place.
3. Refresh and confirm the detected PHY. Repeat for each remaining port.

The root CLI `/usr/local/sbin/ffn-copper-identify` uses the same MP resource:
`begin --port 1` starts a probe, `status` shows its token and candidate, and
`confirm --token TOKEN` records it. `cancel --token TOKEN` abandons the probe.
The generic MP request has resource `copper-identify`, action `apply`, and
payload `{operation, revision, port}` for begin or `{operation, revision,
token}` for confirm/cancel. All mutations are journaled by the MP daemon.

Identification requires a single stable down-to-up PHY transition. Multiple
link changes, no new link, changes to hardware configuration, stale tokens,
reboot, and overwrite of an existing panel mapping are rejected. Probes expire
after 15 minutes. The PHY-to-MAC board wiring is read from the documented vendor
association (PHY16→BCM13, PHY17→BCM28, PHY18→BCM15, PHY19→BCM14); this table
does not establish panel numbering. The corresponding MAC must also be present.
The previous mapping is backed up before an atomic update under the same locks
used by the PHY and faceplate controllers. No PHY registers are written by
identification. Once mapped, the existing faceplate enable/disable and speed
controls operate the matching PHY and MAC, and the copper link service follows
its negotiated speed. This does not mark a VIF packet path as commissioned.

The one-time MP operation `correct-wan-label` migrates only the former single
`port2 -> PHY17/BCM28` mapping to the vendor panel map above, following the
operator's corrected identification. Other mappings and active identification
probes are rejected. Its payload contains only `operation` and current
`revision`; callers cannot supply replacement wiring. The MP first verifies
the DP VIF service is stopped, assignments are empty and no copper packet path
has been commissioned. It backs up and corrects the DP profile before updating
the CP mapping. The CP writes no PHY registers. The MP journal preserves an
uncertain outcome rather than retrying a partial cross-plane change blindly.

Initial validation on 2026-09-16, before cable-pair recovery: MP-authenticated renegotiation on ports 3 and 4
returned verified control readback. Three subsequent samples showed no copper
link or completed partner exchange on either port; the WAN remained linked at
1 Gbps. Neither the control sequence nor these observations certify forwarding.

## Cable-pair recovery and the RJ45 loop

The sysroot `bcm_copper_phy_initialize` routine at
`0x126102ac..0x12610330` programs device 30 register `0x4005` to `2`,
waits 35 ms, writes `0xe4` to `0x4009`, writes `0x52` to `0x4005` and
waits another 35 ms. This is the board's mapping of the four differential
cable pairs inside each copper PHY; the panel-to-PHY/MAC map is unchanged.
The source family also documents the MDI pair-map initialization in
[phy8481.c](https://github.com/Broadcom-Network-Switching-Software/OpenBCM/blob/master/sdk-6.5.16/src/soc/phy/phy8481.c).

Live port 4 initially reported command-data value `0x1b`; ports 1 and 3
reported `0xe4`. Applying the sysroot sequence to port 4, followed by
negotiation, established **10 Gbps PHY and MAC links on the physical 3-to-4
RJ45 cable**. Both MACs report XFI, full duplex and synchronized rates.
Port 1 WAN stayed linked at 1 Gbps. That link-recovery test alone did not
qualify packet forwarding. Subsequent [copper forwarding validation](../bcm/COPPER-FORWARDING.md)
proved bounded bidirectional packet transport, including VLAN-tagged frames;
persistent runtime activation and routed/policy forwarding remain unqualified.

The existing MP resource accepts a standalone
`{"revision":123,"port":4,"restore_pair_map":true}` request. The CP requires
verified identity, firmware `0x1089`, a commissioned mapping, an enabled but
down PHY, valid negotiation observations and no pending transaction. It checks
link again immediately before the command. There is no caller-selected pair
value or raw register access. A failure retains the prior command-data value
in the journal and restores the kernel write gate; generic resolve cannot
silently accept an uncertain pair-recovery operation.

WebUI **Recover copper wiring** is available on eligible down copper ports.
Authenticated FFN-CLI commands use the same MP-owned route:

```text
request platform interface ethernet1/4 recover-pairs
request platform interface ethernet1/4 renegotiate
show platform faceplate
```

Successful recovery stores `pair_maps` in `/etc/ffn/phy.json`. The existing
copper service's `ExecStartPost` restores those settings before saved speed and
admin intent. A matching setting is left alone on a warm service restart;
a changed setting on a linked port is rejected. Simulated cold restoration
and live warm restoration were tested; the latter made zero PHY writes.
No reboot or BCM restart was needed for deployment.

The same authenticated FFN-CLI recovery was then applied to unused port 2.
Its command data now matches the board setting and its restore intent is saved;
with no attached peer it remains down. Ports 3/4 stayed at 10 Gbps and WAN port 1
stayed at 1 Gbps throughout this operation.
