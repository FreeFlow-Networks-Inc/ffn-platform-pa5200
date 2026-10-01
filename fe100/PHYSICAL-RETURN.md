# FE100 routed packet and native exception commissioning

An isolated optical pair on a PA-5220 was configured temporarily at 100G and
restored after every test. The loop used front ports 23/24, BCM ports 34/35,
reserved LIF28, ACL31, next-hop31, LEF31, QMAP31 and source-MAC31. These are lab
allocations, not deployment defaults. Production configuration supplies all
eventual attachment, zone, address, route and translation choices.

The native injector sent four nonce-bearing benchmark packets per phase via
the DP's internal BCM24 attachment. Packets traversed the external fiber into
front23, FE100, and—on a hardware hit—front23 back across the fiber to front24.
The independent oracle compared every Ethernet/IP/transport byte.

Verified on hardware:

- TCP and UDP IPv4 address/port NAT, destination/source MAC replacement and
  TTL decrement, with correct checksums.
- Reverse UDP translation, then deletion returning the original flow to software.
- Explicit drop actions produce no returned or forwarded test packet.
- TTL-one, first-fragment and MTU exceptions return the original, unmodified
  Ethernet packet. Restoring the test next-hop MTU restores hardware forwarding.
- FLOWUNKNOWN misses return the original packet, including after flow deletion.
- The actual MIPS64 native packet owner decodes misses, TTL, fragment and MTU
  exceptions through AF_PACKET into an isolated software endpoint.
- ARP, ICMP echo, IPv6 UDP misses and later IPv4 fragments also return unchanged
  through that native receiver. IPv6 misses can allocate an identity; cleanup
  now journals and removes only the exact, previously absent IPv6 lab key.
- Zero capture drops; exact table/rule/header restoration and unchanged saved
  configuration were checked. Existing production packet owners stayed running.

BCM20 is the FE100 internal return. Its normal TM header processing does not
apply force-forward to these retained ITMH packets. Temporarily selecting RAW
headers and redirecting BCM20 to BCM24 delivered the complete original-packet
envelope. Both the header mode and forwarding destination must be journaled,
read back and restored together; SDK success alone did not prove delivery.

The measured envelope has a four-byte OTMH, four-byte retained ITMH, sixteen-byte
common header, eight-byte message information, an optional sixteen-byte IPv4
or forty-byte IPv6 FLOWUNKNOWN key, eight-byte packet information, then Ethernet. Type8/code0 uses
the key; type5/code2 (TTL), type5/code13 (MTU) and type4/code30 (first fragment)
omit it. Exception messages lack a zone field, so the attachment's current
front/LIF binding supplies the zone. A packet cannot select another binding.
Type4/code32 carries the qualified ARP/ICMP original-packet exceptions.

Miss-only tests may cause FE100 to allocate an identity entry. Preparation now
journals the exact preflight-empty key, so cleanup removes learned identities
even when no forwarding action was installed. Pre-existing entries are refused.

These are small functional tests, not a 100G throughput result or general
production qualification. Deployed bidirectional attachment paths,
aggregate/VLAN mappings, complete software fallback, accounting/aging and the
commit/lease withdrawal coordinator remain prerequisites for production
admission. The live session observation feed still cannot install flows.

The subsequent 256-packet UDP PNAT test verified every rewritten frame and
captured four native FLOWSTATS reports. Each report contains a delta of 64
packets / 8,256 bytes; together they match all 256 original 129-byte Ethernet
frames. See [COUNTERS.md](COUNTERS.md) for the actual compact format and its
remaining lifecycle integration requirements.

The paired lab mode subsequently installed both UDP/TCP NAT entries together
and interleaved their tuples. UDP verified hardware translation, drop and
deletion-to-software for both entries; TCP verified all 256 rewritten frames
and separate hardware counters for both directions. These share one isolated
ingress/egress loop and do not yet qualify separate production interfaces or
aggregate member changes. A failed second-direction install remains journaled
so recovery removes both exact owned entries before restoring their resources.
