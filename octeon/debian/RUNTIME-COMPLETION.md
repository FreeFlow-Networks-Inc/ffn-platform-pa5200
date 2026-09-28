# Native runtime deployment completeness

The MP installs and owns the selected platform adapters. CP and DP images must
include the entire role overlay, not just readiness agents. `overlay.json` and
`test_runtime_overlay.py` describe and check those dependencies.

The DP image now requires conntrack events, labels and netlink, plus the nftables
NAT route lookup and distribution expressions. Image validation rejects kernels
without these features. Diagnostic Debian packages are checked independently of
kernel enforcement. The Security supervisor starts after the isolated network
namespace and keeps transit closed until a current policy generation is verified.

For an existing deployment, preserve its running/candidate configuration, SSH
identities and selected providers. Apply the core NAT, Security and policy
installers to their documented manager/daemon paths; they merge the configd hooks
as well as copying modules. Updating only the WebUI directory leaves an older
configd import tree in use. Merge the platform CLI extension and policy CLI hooks
into the existing console shell so its control-daemon authentication remains intact.
Install the native `octeon/debian/*-mp` command wrappers used by selected MP
resources. They are code dependencies, not customer configuration.

Before selecting a policy provider, run the core `test_nat_namespace.py`,
`test_security_namespace.py` and `test_security_runtime_namespace.py` on the actual
DP in disposable namespaces. These exercise return translation, ordered rules,
revocation, counters, durable sessions, lease expiry, collector restart and event
loss recovery. Unit tests alone do not commission a packet provider.

`build-policy-modules.py` can add missing standalone expressions against the exact
running kernel build tree. It does not change Kconfig or bypass unresolved kernel
symbols. Some missing features, including conntrack netlink on a kernel without
its required exports, require `build-security-kernel.py` and a controlled DP boot.
Keep the previous kernel and its modules available for recovery; rebuild the
platform packet drivers for the new kernel too.

WAN-only deployments now recover their internal packet link and staged packet
initialization through the MP when a committed attachment is reapplied. Recovery
checks the DP boot identity, refuses faulted/active incomplete engines, drains
hardware sessions and repeats wire qualification before enabling the attachment.
A missing trunk is reported as unavailable readiness, rather than making the
entire status request fail. Aggregate owners retain their existing recovery path.

NAT64/NPTv6 and FE100 action codecs are not evidence of production hardware
forwarding. Their commissioning gates remain in effect. QoS, PBF and decryption
also require their respective enforcement providers; installed controls do not
claim those providers are qualified.

Candidate policy validation and first aggregate Commit
----------------------------------------------------
Security/NAT validation may use the selected platform's candidate aggregate
inventory for nftables check-only compilation. Address objects are resolved
from the submitted configuration; no customer mapping is written and no link,
route, policy or LACP owner is created. New aggregates are reported as pending
interfaces, never acknowledged runtime bindings. DHCP addresses are not guessed.

Apply, replay and the forwarding lease always rediscover real owners and require
current-boot LACP/network evidence. A successful candidate check therefore does
not mean the aggregate is active. Configd must still observe its commissioned
attachment before activating the policy. Unsupported actions or inspection and
logging requests continue to block validation.
