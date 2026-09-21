# Policy kernel prerequisites

The 2026-09-21 live readback of the commissioned MIPS64eb DP kernel showed
`NFT_NUMGEN`, `NFT_HASH`, `NET_SCH_HTB`, `NET_SCH_FQ_CODEL`, and `NET_CLS_FW`
disabled. Installing nft or tc userspace does not supply these kernel features.

Merge `policy-kernel.config` into an **isolated copy of the exact commissioned
kernel source/configuration**, preserving its hardware patches and generated
symbol versions. Run olddefconfig, verify each requested symbol, build the
kernel and modules, and retain the configuration, source revision, compiler
version, vermagic and artifact hashes. Do not rebuild from the older generic
plane candidate and assume it matches the commissioned packet-fabric drivers.

Matching modules may be loadable without a reboot only when their ABI and
dependencies match the running kernel. Otherwise stage a new DP kernel and
plan a DP restart with recovery verification. Do not replace boot artifacts or
autoload modules before validating their compatibility.

Run core `tests/test_nat_namespace.py` on the target using disposable namespaces.
This includes real round-robin and address-hash destination NAT, translated UDP
ports, reply translation and connection affinity. A compiler pass is not an
activation acknowledgement. HTB, PBF and transparent proxy features also require
their own providers and packet tests; this fragment does not implement them.

The core dataplane-tools report includes kernel configuration evidence separately
from executable availability. `compiled` never means `runtime_verified`.
