# FE100 IPv4 NAT action implementation

The session codec and native adapter now encode and read back IPv4 NAT and port
translation actions. `ffn_fe100_nat.session_pair4` consumes the kernel-selected
original/reply TCP or UDP tuples and two verified next-hop indices. It allocates
no addresses or ports and contains no appliance interface, gateway or customer
configuration. Source NAT, destination NAT and combined translation use the
same reversible pair construction. Plain untranslated pairs retain ordinary
routed forwarding with TTL decrement.

The forward action emits the reverse of the kernel's reply tuple; the reverse
action emits the reverse of its original tuple. SessionManager verifies both
relationships before any hardware write. Existing durable intent, two-direction
readback, ambiguous-write rollback and exact owned-entry removal also apply to
NAT entries. Interrupted ADD followed by UPDATE remains recoverable.

## Reference audit

Inspected the VM sysroot on 2026-09-21 without loading vendor modules or opening
hardware. Paths below are relative to its `opt/dpfs` directory:

* `usr/lib/python2.7/site-packages/pcs/packets/fe100.py`, `flowEntry`, lines
  480–536: the flow key is 16 bytes, state is 32 bytes, then translated source
  address, destination address, source port and destination port. FFN retains
  its 64-byte record with four trailing zero bytes.
* `usr/local/lib64/libpandp_cp.so.1.0`, SHA256
  `b57227a460144c8c2545fc2e268b31f475ef72ac2a6f1d457387ab46d842c3e9`:
  DWARF places the native IPv4 key at byte 16, state at 32 and NAT union at 64.
  `condor_nat4_state_s` is 12 bytes: ports at offsets 0/2, addresses at 4/8.
  The containing NAT union is 36 bytes; the full flow union is 144 bytes.
* `usr/lib/python2.7/site-packages/cp/_condorlib.so`, static disassembly of
  `init_condorlib` at 0x1002e648–0x1002e6d4: NAT mode constants are NONE=0,
  NAT=1, PNAT=2, VER=3. No Python extension was executed to recover constants.
* `pan_flow_condor_add_state` at 0x105d7890–0x105d78cc and
  0x105d7d10–0x105d7d24: NAT starts at mode 1; either unequal reversed port
  selects mode 2. IP-version translation selects mode 3 and remains rejected
  by this implementation.

The adapter explicitly rearranges NAT fields at the wire/native boundary.
Inactive NAT storage and the inactive IPv6 union tail are ignored on readback;
active IPv4 translation must match exactly. TCP sequence adjustments,
recirculation, IPv6/version translation and other unimplemented flags remain
rejected.

## Qualification and operational visibility

Encoding support does not commission the ASIC. Native insertion of a NAT
action additionally requires `nat_offload_verified` from the trusted hardware
qualifier. The deployed policy controller still rejects production admission;
its read-only status exposes implementation capabilities through MP controld.
Use `show platform fe100 capabilities` from FFN-CLI. The same status resource
is available to the management UI through its existing daemon operation.

Live Internet traffic continues through the supervised OCTEON kernel Security
and NAT provider. This update does not reset FE100, rewrite BCM routes, restart
LACP, flush conntracks or install hardware NAT entries.

The MIPS64 unit suite covers exact wire/native vectors, address and port
translations in both directions, wrong reverse-pair rejection, independent NAT
qualification, accepted writes with lost replies, recovery and policy fencing.
Remaining hardware qualification requires isolated bidirectional packet tests
checking translated bytes, IPv4/TCP/UDP checksums, TTL, deletion, restart and
counter accounting before production session admission can be enabled.
