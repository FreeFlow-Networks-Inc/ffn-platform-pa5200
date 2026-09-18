# DP internal 40G link bring-up

The PA-5220 CN78XX BGX2/LMAC0 internal XLAUI link is now enabled and linked.
DP reports block lock, no fault, and port-kind 8. The independent BCM port
inventory reports internal port 24 enabled/link-up at 40000 Mb/s; ports 25
and 26 remain link-down. This identifies a working internal link, not a
front-port-to-front-port forwarding test.

## Implementation

- `octeon/kctl/ffn_dp_link.c`: optional CN78XX-only kernel observation module.
  Reads BGX2/LMAC0 and PKI registers without allocating resources or writing
  configuration. STATUS1 uses two reads for its latching-low link indication.
  The old `ffn_pki/status` hook runs allocator exercises on read; this new
  probe does not use that hook.
- `ffn_dp_link.py`: validates the board link layout, requires completed Debian
  boot, refuses commissioning with PKI active or an unexpected partial state,
  and invokes only the existing audited kernel BGX2 enable routine. It holds
  the fabric ownership lock, avoids reinitializing an enabled LMAC, and reports
  write failure separately from observed link state. Other SDK/kernel users
  must still be coordinated; that advisory lock cannot serialize uncooperative
  hardware owners.
- `ffn_dp_agent.py` adds fresh `packet_io` observations to the authenticated
  management handshake. The WebUI shows internal-link readiness separately
  from physical forwarding verification. Missing/invalid observations become
  unknown, not link-down or successful offload.
- `build-dp-link.sh` builds against an explicitly supplied configured kernel
  and compiler prefix, with warnings treated as errors and a BE-MIPS ELF check.

The running module was compiled against `6.18.49-ffn-debian-dp-20260914+`.
It is installed in that kernel's `extra` modules directory. The optional
`ffn-dp-link.conf` loads only the read-only probe at boot. Link enable is not
automatically run at boot; it remains an explicit commissioning operation.
The working kernel helper initializes additional PCS configuration; this
wrapper does not claim to restore all of that configuration after an error.

## Validation and current limits

- Strict kernel-module build passed and the module loaded on the actual DP.
- Before: link disabled, no block lock, pknd 0, PKI disabled.
- After: enable/RX/TX/link/block-lock all 1, fault 0, pknd 8, PKI disabled.
- The switch independently reports port 24 linked at 40 Gb/s.
- Fifteen Python tests passed on the actual MIPS64 DP; management UI tests
  passed. DP `systemctl --failed` still reports zero units.

PKI is **not enabled** and there are no initialized packet pools in the
existing FPA3 bookkeeping. No packet DMA, SSO consumer, PKO egress queue or
FE100 flow-table activation was commissioned by this change. Both
`physical_packet_transport_verified` and `session_offload_verified` remain
false. The next work is verified buffer ownership, bounded queue setup and
recovery before admitting traffic into PKI. The existing FPA3 debug hook uses
permanent bootmem allocations, so its initialization cannot be treated as an
ordinary reversible settings change.
