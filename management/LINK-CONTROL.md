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
shows PHY addresses separately from faceplate port numbers. Port 2 to PHY 17
was confirmed by the appliance operator on 2026-09-11; the other three copper
port mappings are not yet verified. PHY settings alone do not prove MAC speed
synchronization or WAN forwarding. Copper faceplate speed control remains gated
until that integration is commissioned.

Install the CP PHY helper beside ffn_mdio.py and the copper service drop-in to
restore saved advertisements after firmware initialization. Local recovery after
inspecting an uncertain PHY operation is `ffn_phy_control.py resolve`; this
explicitly accepts observed advertisement state. Faceplate state has its separate
existing resolve command and boot restore unit.

SDK reference: https://github.com/Broadcom-Network-Switching-Software/OpenBCM/blob/master/sdk-6.5.16/include/bcm/port.h
PHY register reference: https://github.com/Broadcom-Network-Switching-Software/OpenBCM/blob/master/sdk-6.5.16/src/soc/phy/phy8481.h


## BCM activation and restart recovery

The BCM daemon must be restarted once after upgrading its SDK link handlers.
A restart interrupts switch links and reinitializes the ASIC; it does not reboot
MP, CP or DP. `ffn-bcmd.service` also starts `ffn-front-ports.service` so starting
from a stopped or failed state initializes the faceplate. The DMA prerequisite
accepts the allocator confirmation from either dmesg or the current boot's kernel
journal; a command-line reservation token alone is never sufficient.

Link ability parsing accepts complete SDK output lines only. CINT echoes input
including printf literals for unsupported rates, which must not become advertised
capabilities. Copper MAC rate control remains unavailable pending synchronization
with the external PHY. PHY negotiation alone does not prove WAN forwarding.

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
The current measured mapping is ethernet1/2 to PHY 17. An administrator can store
additional measured associations in `/etc/ffn/copper-map.json`: an object whose
keys are physical port numbers 1 through 4 and whose values are distinct integer
MDIO addresses 16 through 19. For example, the currently established mapping is
`{"2":17}`. This file replaces the default map; include every verified association.
Unknown mappings disable copper writes instead of silently controlling a different
socket. Inventory revisions incorporate mapping and PHY configuration changes.

All four port mappings are covered by controller tests. Live validation reapplied
port 2's Auto advertisement through the authenticated CLI and MP daemon, leaving
its 1G external link and advertisement unchanged. Ports 1, 3 and 4 still need a
physical cable/link correlation before commissioning their mapping on this unit.

The faceplate page reports copper wire speed/link separately from switch-side
link. A wire link alone does not establish PHY-to-MAC synchronization, a dataplane
attachment, forwarding, or DHCP service. MAC rate synchronization remains pending.
The GPIO/PHY bring-up service retains independent lifecycle control; BCM process
restart must not reset copper firmware or negotiation.
