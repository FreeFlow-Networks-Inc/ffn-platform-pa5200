# Copper VIF driver

`ffn_copper_vif.py` extends the OCTEON Linux TAP VIF driver. The MP daemon
reads the existing faceplate controller and delivers PHY/MAC observations to
the DP VIF owner. WebUI and CLI configuration continues through the MP's
`vifs` resource; neither frontend writes MDIO or switch registers.

The DP profile `/etc/ffn/vif-copper.json` contains version `1` and a `ports`
object. Each commissioned physical port key (`1` through `4`) has integer
`phy` (16–19), integer `bcm_port` (28, 13, 14 or 15), and boolean
`packet_path_verified`. PHY and MAC assignments must each be unique.
There are no guessed copper mappings. A mapped port becomes selectable only
after its packet path has been separately commissioned and verified.

The operator corrected the WAN label: port1 maps to PHY17/BCM28. The complete
PA-5220 panel order is port1=PHY17/BCM28, port2=PHY16/BCM13,
port3=PHY19/BCM14, port4=PHY18/BCM15. The former single port2 label is migrated
through the MP `copper-identify` resource with stopped, empty VIFs. The corrected
profile retains `packet_path_verified=false` until wire forwarding is qualified.
Do not mark packet paths verified based on link alone: BCM/FE100 queue,
ingress/return-header and OCTEON packet transport setup must also be tested.
Profile changes require stopping the VIF owner and restarting it after
review; they are not hot-reloaded underneath existing bindings.

Copper TAPs attach without carrier. Carrier and packet delivery require a
fresh observation with matching wiring, enabled PHY and MAC, no pending PHY
transaction, both links up, and matching 100/1000/10000 Mbps rates. A single-use
DP challenge expires after 30 seconds measured from issuance on the DP's
monotonic clock. Delayed, duplicate and out-of-order responses cannot extend
that window. Loss of the MP/CP observation path lowers carrier and drops
copper traffic. Driver shutdown also lowers TAP carrier.

Commissioned copper3/4 now also requires current CP-owned BCM forwarding
readback. See `management/COPPER-NETWORKING.md` for control ordering,
qualification lifetime, recovery and the physical bridge/routing tests.

Install `management/install-copper-vif.py` from a staged directory as
described in its module documentation. It requires stopped, empty VIFs,
backs up replaced files, learns only verified physical mappings and leaves
new packet paths uncommissioned. It enables the MP observation timer, not
forwarding. No reboot is needed. Existing optical packet paths retain their
commissioned mapping and forwarding behavior.

Validation: `test_copper_vif.py` covers mapping collisions, packet headers,
link loss, admin disable, speed mismatch and observation expiry/replay.
`test_copper_vif_kernel.py` verifies attach, carrier transitions, expiry and
close on the real OCTEON MIPS kernel in a disposable network namespace,
without sending physical packets. `test_copper_vif_handshake.py` exercises
the running driver's control socket, replay rejection, link loss and lease
expiry against real TAP carrier on an isolated dummy trunk.
`test_vif_kernel.py` verifies existing
L2 bridge, L3 VRF, static routes, policy routing and drift checks. These
software/kernel tests do not certify copper wire forwarding or throughput.
