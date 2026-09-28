# Native Debian packet runtime

The CP and DP native image overlays include the existing copper rate follower,
network adapter, WAN attachment, aggregate/LACP owners and their dependencies.
Driver and agent readiness alone does not establish a front-panel packet path.

On boot, CP starts the copper follower timer. DP starts the isolated network
namespace and aggregate recovery timer. A missing network state file is created
atomically with every front port disabled and no routes. Existing state is
validated and preserved; invalid state is an error, not a factory reset.
WAN/VIF/aggregate packet owners still require MP configuration and qualification.

The MP installation must include `octeon/debian/ffn-network-mp` as
`/usr/local/sbin/ffn-network` and register the commands provided by
`install-control-channel.py`. Preserve the appliance's commissioned copper PHY
mapping in `/etc/ffn/copper-map.json`; never infer it from link state or use a
customer's address configuration as an image default.

BCM may initialize its empty internal packet trunk in ETH or RAW mode. The
existing journaled fabric initializer accepts both, checks that there are no
queue bundles, transitions through TM to TM_SSP, and verifies each allocation.
An occupied trunk or uncertain allocation remains blocked. Inspect the current
BCM epoch, allocation journal and SDK readback before reconciling an unknown MP
request; do not delete journals or repeat allocations blindly.

Verify recovery in order:

1. PHY mapping, administrative state, negotiated rate and switch MAC agreement.
2. BCM lifetime, trunk header and queue readback.
3. Current DP boot identity and return frames from the intended front port.
4. MP-controlled packet attachment and configuration apply status.
5. Address/route readback and policy-controlled traffic from configured clients.

Wire qualification is not DHCP lease acquisition, internet reachability or
hardware NAT verification. The WAN packet owner reports hardware offload false.
An empty LAN/security/NAT configuration must not be replaced by test defaults.
