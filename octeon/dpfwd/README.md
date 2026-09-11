# dpfwd -- the OCTEON dataplane forwarder

FFN's own packet forwarder for the OCTEON dataplane: parse, flow table, policy
lookup, L3 routing and neighbour resolution, plus the packet-I/O backends for
OCTEON-II (IPD/PIP + POW + PKO) and OCTEON-III (PKI + SSO + PKO3).

On a PA-5220 this runs on the 40-core CN78XX. The forwarder itself has no chip
headers, so the same sources compile natively on x86 and cross-compile for the
target; only the I/O backend differs on real hardware.

## Byte order is the standing hazard

The target is **big-endian MIPS64** and every development machine is not. The
L3 and ARP code is the most exposed -- IPv4 addresses, the RFC 1624 incremental
checksum update, MAC and ARP field layout -- so `make check` runs every suite
under `qemu-mips64` as well as natively. A change that passes only on x86 has
been tested on the one byte order the product will never see.

### qemu is the convenient check; the CP is the real one

`make check` uses `qemu-mips64`, which is right for CI and for a workstation.
But the appliance already contains a big-endian MIPS64 machine that runs a full
Debian userland -- the **control plane** -- and running the suites there tests
the actual silicon, the actual kernel and the actual ABI rather than an
emulation of them. Both L3 suites have been run that way:

```
host: Linux 6.18.49-ffn-debian-cp-dirty mips64
ffn_dp_l3_test (big-endian)      PASS: all L3 tests
ffn_dp_l3_v6_test (big-endian)   PASS: all IPv6 L3 tests
```

The suites are self-contained -- no CVMX, no hardware -- so a **static** build
runs on the CP regardless of which root it is using. Recipe, on the RE VM that
holds the toolchain:

```sh
CC=/mnt/clones/openwrt-toolchain/toolchain-mips64_octeonplus_64_gcc-13.3.0_musl/bin/mips64-openwrt-linux-musl-gcc
F="-O2 -g -Wall -Wextra -Wno-unused-parameter -std=gnu11 -static"
$CC $F ffn_dp_l3.c ffn_dp_l3_config.c ffn_dp_l3_parse.c ffn_dp_arp.c       ffn_dp_l3_test.c -o l3_test.be
$CC $F ffn_dp_l3_v6.c ffn_dp_l3_v6_config.c ffn_dp_l3_parse.c       ffn_dp_l3_v6_test.c -o v6_test.be
```

`file` must say **ELF 64-bit MSB**; the binaries also print their own endianness
at startup, so a little-endian build cannot masquerade as a passing big-endian
run. Copy them into the CP's root on the MP (`<cproot>/root/`) and run them
through `ffn-cp`.

Two traps on that path. The RE VM can *reach* the MP but has no key for it, so
the copy relays through the workstation -- and `scp ... | tail` returns tail's
exit status, so a failed copy reports success unless the status is checked
directly. `STAGING_DIR not defined` from the OpenWrt gcc is a warning about
its own sysroot search and does not affect a static build.

## Building

    make            native build (x86-64)
    make check      every suite that needs no OCTEON hardware, both byte orders
    make l3-test    L3 routing / FIB
    make arp-test   neighbour resolution
    make mips64     cross-build for OCTEON
    make oct3-cvmx SDK=/path/to/OCTEON-SDK
                    compile the hardware backends against the real CVMX API

`oct3-cvmx` compiles only -- it cannot run without hardware -- but it holds the
CVMX call sites to the real API. Every fault it has caught (an invented
sub-descriptor code, a made-up accessor, a call with the wrong arity, a hook
that was never defined) would otherwise have shipped as a dataplane that
builds, runs, and moves no packets.

## Layout

| file | what it is |
|---|---|
| `ffn_dp_oct.c/.h` | the forwarder: parse, flows, policy, dispatch |
| `ffn_dp_l3.c/.h` | FIB, neighbour table, header rewrite |
| `ffn_dp_l3_config.c/.h` | applies relayed `dp.l3.*` config keys |
| `ffn_dp_arp.c/.h` | ARP request/reply, rate limiting, ageing |
| `ffn_dp_io_octeon*.c` | OCTEON-II and OCTEON-III packet I/O |
| `ffn_dp_io_afpacket.c` | AF_PACKET backend, for veth testing on Linux |
| `dp_serve.c` | interop harness: the real command loop over an mmap'd file |
