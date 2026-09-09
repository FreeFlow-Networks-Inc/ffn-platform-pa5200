# Owner VM driver references

Root: /mnt/clones/5220-sysroot1-full/opt/dpfs on the build VM.
No owner binaries or firmware are included here.

* lib/modules/3.10.87-oct2-dp/kernel/drivers/pan/fe100/fe100.ko:
  inspected probe at .text+0x500 and ioctl at .text+0x108. Probe handles
  PCI, interrupts and character-device setup. Ioctls 0x101/102/103
  acknowledge, disable and enable user interrupts; this is not a packet queue.
* usr/share/pdt/fe100.py:3362: SPM slot/port/priority mapping. Four owner
  fe100_cfg1 maps are at offsets 1336,1352,1368,1384. Entries with 255 are
  skipped. The two-byte BE ABI packs slot at bits13:10, logical port9:6,
  priority5:4 and mapped port3:0.
* usr/share/pdt/fe100.py:4515: TMI capture at 0x8300/0x8200. Sources 2=ILTX,
  0=EGR, 1=IPQ. Sixteen capture words at 0x8304..0x8340 form 64 bytes in
  big-endian word order despite little-endian MMIO. The helper restores
  source/control settings and avoids fault W1C bits.
* usr/share/pdt/fe100.py:4890: LIF type5=SYSPORT, fwd_key=destination.
  Mask one means compare. Owner DWARF defines a 36-byte entry: payload
  words4/8/12, ten-byte mask16 and key26. Ingress port is key bits37:32.
  pan_fe100_get_access_tbl dereferences PAN process state, so the adapter
  supplies an explicit audited table selector.
* /mnt/clones/openbcm/sdk-6.5.26-DNX.1/include/soc/dpp/headers.h:
  Jericho ITMH direct destination encodes the system port after prefix1.
  Captures 01000f00 and 01000800 confirm the SPM correction.

Runtime usr/local/lib64/libpandp_cp.so.1.0 is pinned to SHA-256
b57227a460144c8c2545fc2e268b31f475ef72ac2a6f1d457387ab46d842c3e9.
Helpers reject a different ABI. Calling condor_strerr(3) against this
library returned NOTFOUND before the empty LIF slot was used.
