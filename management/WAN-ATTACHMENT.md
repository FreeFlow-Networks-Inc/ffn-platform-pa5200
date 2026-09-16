# Committed WAN address and local service attachment

The PA5200 configd adapter accepts an interface management profile under
`layer3`, resolves it from committed XML, and sends permissions with the address
to the DP network owner through controld. An empty Layer 3 editor placeholder
does not activate an unattached port. Unsupported modes/profiles remain errors.

For a configured copper WAN1 address, configd requests `wan-path attach` when
the packet owner is absent. CP validates the existing WAN wire qualification
against its BCM lifetime and the DP boot identity, checks queues and the return
header, and enables its owned WAN1-to-trunk redirect. A DHCP Offer is not a
prerequisite for a static address. Qualification still requires observed frames
with the commissioned WAN1 source metadata in the current CP/DP lifetime.

The DP service exclusively locks the packet fabric and attaches only p1 to
the direct OCTEON trunk. It does not relay packets through the MP, alter copper
3/4 forwarding, or claim hardware session offload. Local traffic reaches the
kernel interface-profile INPUT rules; transit traffic retains ingress inspection.
Short Ethernet frames are padded before adding headers that BCM removes.
Temporary TAP-down events during address changes drop packets without killing
the transport owner. State includes the process start time and DP boot identity.

Default virtual-router static routes are applied on DP, not the MP management
network. Until the core VR editor migrates legacy SQL definitions into XML,
the adapter also reads those saved definitions. Once `ffn-candidate-managed`
is present, XML is authoritative, including deletions. Other virtual routers
require an explicit DP VRF implementation and are rejected by this adapter.

Operational commands use the authenticated MP daemon:

```
show platform wan-path
request platform wan-path attach
request platform wan-path detach
```

Install `ffn_wan_runtime.py` and `ffn-wan-attachment.service` on DP, update the
CP WAN owner and MP backend/configd adapter, and install the core interface
management module on MP and DP. The attachment is explicitly selected after
qualification; it is not blindly replayed at boot. An applied interface/profile
does not prove upstream reachability: inspect ARP, destination MAC, return route
and real request/reply packets before reporting Internet connectivity.
