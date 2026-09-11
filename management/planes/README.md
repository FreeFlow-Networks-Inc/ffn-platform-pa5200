# PA-5200 plane daemon profile

Use this profile with the matching FFN-NGFW plane-control implementation.
The core supplies `ffn_planed.py`, `ffn_linux_network.py`, the systemd template,
and `image/install-plane-node.sh`. The PA-5200 module supplies the node topology,
CP NIF adapter, DP fabric policy and UI. Install core code on all three nodes
before replacing the DP's `ffn_network.py` with the thin platform adapter.

1. Install `mp.json`, `cp.json`, and `dp.json` as the corresponding role's
   `/etc/ffn/planes/ROLE.json` using the core installer. It does not start units.
2. On CP, install `management/ffn_nif_control.py` as
   `/usr/local/sbin/ffn_nif_control.py`. It requires the existing commissioned
   `ffn-fe100-links.service`, its hash-pinned bring-up dependencies and the
   `ffn_fe100_links_status.py` read-only observation tool.
3. On DP, install this module's `octeon/debian/ffn_network.py` wrapper as
   `/usr/local/sbin/ffn_network.py`. Keep the existing network boot service,
   configuration and fabric ownership. Runtime requests for enabled ports or
   routes outside the currently attached physical backend are rejected.
4. MP uses the existing pinned `ssh-cp.conf`. CP needs its own provisioned DP
   credential and verified DP host key. `ssh-dp.conf.example` describes paths;
   it contains no keys and must be installed as `ssh-dp.conf` only after those
   paths are provisioned. Do not copy the MP private key or disable host-key
   checking. Restrict the DP credential to the fixed plane client command.
5. Start `ffn-plane@dp`, `ffn-plane@cp`, then `ffn-plane@mp` on their respective
   nodes. Configure the manager's `FFN_PLANE_SOCKET` to the MP socket and restart
   only the manager. Existing provider network operations then traverse both
   control daemons; other resource adapters retain their previous paths.

The core Control Planes page operates on network configuration. Device > NIF
Links is declared by this module and issues the same core protocol for resource
`nif`. The CP executes that resource locally; network requests continue to DP.
An enable request is `{"revision":N,"enabled":true}`. The adapter requests a
start of the existing service without restarting an active service or blocking
for its full bring-up time. Result activation remains pending until status is
checked. Service state, physical link observations and forwarding readiness are
separate. NIF disable is rejected because no commissioned shutdown transaction
is available; no raw register operation is accepted through the API.

Do not enable competing legacy apply agents for interfaces owned by this path.
This profile does not activate the daemons, change links, allocate BCM queues,
restart forwarding or provision SSH credentials merely by being included in a
build. Hardware deployment should verify the network service, fabric attachment,
read-only status through all hops, then a validated port change and packet tests
using the appliance's documented commissioning topology.

The four-port relay and lack of full hardware offload remain limitations. The
control daemon architecture is usable on OCTEON; it does not replace the existing
packet relay with a direct ASIC datapath.
