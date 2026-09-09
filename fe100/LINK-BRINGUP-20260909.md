# PA-5220 FE100 link bring-up, 9 September 2026

Hardware validation on the owner's PA-5220: the Sesto gearbox, FE100 NIF
100G Ethernet interface, and FE100 TMI Interlaken interface initialize with
return code 0. BCM port 3 reports 100G up; port 20 reports ILKN up at 12.5G
per lane, configured for 12 lanes. This establishes physical links, not
parser, flow processing, firewall offload, or packet forwarding readiness.

## Root cause and runtime

Linux `mdio-cavium.c` used the register number instead of the device address
in the Clause 45 read command. Patch `0013-mdio-cavium-c45-device-address.patch`
restores valid IDs on both CP MDIO buses. Bus 1 PHY 0 identifies as
`ae02:5290`; the owner's standalone initializer recognizes Sesto.

`ffn_mdioctl` exposes `/dev/ffn-mdio1` with root-only, locked Linux bus access.
Gearbox writes require a separate `allow_gearbox_writes` gate and are confined
to PHY 0. Copper writes remain separately gated on bus 0 PHYs 16 through 19.

`ffn_gearbox_mdio.c` adapts the owner's `pan_read_gearbox_register` and
`pan_write_gearbox_register` callbacks to this ioctl. The inspected callback
register format is `(devad << 16) | register`. `bcm_gearbox_phy_initialize`
takes one unsigned 32-bit PHY address and does not open another BCM SDK
session. The write-denied dry run reached the expected first reset write
at `1:0x8200`, proving callback interposition before enabling writes.

The owner's VM reference is `/mnt/clones/5220-sysroot1-full` (plural
`clones`; `/mnt/clone/5220-sysroot1-full` does not exist on the tested VM).
Runtime libraries are used in place on CP under
`/opt/ffn-compat/tmp/dpfs/usr/local/lib64`. No owner firmware/library is
included in this repository. Inspected library SHA256 values:

* `libpanbcm_cp.so.1.0`: `8e4a786a901903a5806edffc213a0f0727167e5f7b7a8b0a145bf2d94aea2040`
* `libpandp_cp.so.1.0`: `b57227a460144c8c2545fc2e268b31f475ef72ac2a6f1d457387ab46d842c3e9`

## FE100 adapter and measured configuration

The little-endian MMIO adapter permits only explicitly allowed aligned
registers in one 32-KiB block. Default build permits NIF; compiling with
`-DFFN_BLOCK_BASE=0x8000` produces the separate TMI adapter. Each is loaded
before the owner's library to interpose `fe100_reg_rd/wr`. Both write-denied
dry runs were checked on hardware before actual initialization.

The inspected DWARF defines `pan_fe100_cnf_t` as 2812 bytes with NIF at
2476 and TMI at 2576. NIF uses 100G/Qumran. TMI uses 12 lanes at 12.5G,
in-band flow control. The initial identity-lane/basic-segmentation profile
established a physical link but failed packet tests. The later inspection of
the owner's exported `fe100_cfg1` established the correct profile:
single-channel mode, enhanced segmentation, 128/256/64-byte
minimum/maximum/short bursts, and lane order
`11,10,9,0,8,4,5,6,1,2,7,3`. The current link initializer uses these values.
BCM's configuration corroborates lane count, rate, maximum and short bursts.
See [packet-path progress](PACKET-PATH-20260909.md) for the traffic results.

Measured after both initializations:

| Register | Value |
|---|---|
| NIF MAC PCS status `0x10030` | `0x10` |
| NIF core PLL `0x10a14` | `0x1` |
| TMI IL status `0x8014` | `0x15fff` |
| TMI init status `0x8010` | `0x10003f` |
| TMI core PLL `0x8614` | `0x1` |

Prior to gearbox initialization NIF timed out polling PCS bit `0x10`.
After gearbox initialization NIF succeeded and BCM ce3 came up. TMI then
succeeded and BCM il20 came up. All six cabled front ports stayed up.

## Automatic startup and remaining work

`ffn-fe100-links.service` follows `ffn-copper.service` and runs gearbox,
NIF, then TMI initialization. It checks both BCM links and FE100 PCS status.
It uses external process timeouts and clears the gearbox write gate both
in the shell exit trap and systemd `ExecStopPost`. The Python alarm alone
cannot guarantee interruption of a blocked foreign-function call; use the
provided service/shell wrapper for bounded execution.

CP traces live in `/var/lib/ffn/fe100/boot-{gearbox,nif,tmi}.txt`.
Read current status with `python3 /usr/local/sbin/ffn_fe100_links_status.py`.
The complete guarded CP/DP restart was tested after installation: all four
hardware services are enabled, both FE100 links and all six cabled front
ports recovered automatically, CP and DP both report systemd `running`,
and both MDIO write gates are off. [Captured FE100 status](VALIDATION-20260909.txt)
and [front-port/MDIO checks](../bcm/FRONT-PORTS-VALIDATION-20260909.txt) record
the post-restart result. This was a processor restart, not a power-removal test.
This service intentionally initializes physical interfaces only. Remaining
work includes operational LIF/forwarding entries, system-port mapping, and
validating the complete FE100 return path. The newer eight-queue lab recipe
has passed repeated front-port and NIF Ethernet-loopback bursts. Parser,
NIF port-map and TLU partition initialization also succeed; these remain lab
operations rather than an enabled packet-processing startup service.
