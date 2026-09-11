# The dataplane forwarder runs on the DP

**Built, deployed and executing on the CN78XX.** `ffn-dp-octeon` is an OCTEON
userspace application: the SDK's `cvmx_user_app_init()` forks it onto every core
in the coremask and it drives PKI/SSO/PKO3 directly. First run on the live
appliance:

```
CVMX_SHARED: 0x10210000-0x102f0000
Active coremask =  node 0: 0xffffffffff          40 cores
interfaces: 10   (ipd_port 0xa00 = iface 2 index 0 on CN78XX)
iface  mode                         ports  ipd(0)   pko_dq
2      XLAUI                        1      0xa00    -1
7      ILK                          8      0x500    -1
8      NPI                          64     0x100    -1
9      LOOP                         4      0x0      -1
0,1,3,4,5,6  DISABLED
```

Interface 2 is the 40G XLAUI to the BCM88375 — the one that matters, and the one
[`ffn-bgx-driver-cvmx-generation`](DP-BRINGUP.md) brought up at L1. `pko_dq`
reads `-1` because the probe path never calls `cvmx3_hw_init()`, which is what
resolves the descriptor queue via `cvmx_pko3_get_queue_base()`.

## Building it (`dpfwd/build-octeon-app.sh`, on the RE VM)

Toolchain and SDK, both used in place and never written to:

```
SDK  /mnt/clones/sdk51/OCTEON-SDK
TC   /mnt/clones/openwrt-toolchain/toolchain-mips64_octeonplus_64_gcc-13.3.0_musl
```

musl static, not the SDK's own compiler: the SDK's bundled 2012 glibc dies in
`ptmalloc_init` on a modern kernel before `main()`. Output is a 3.5 MB
`ELF 64-bit MSB` static binary.

### `octeon-app-init.h` is a COMPILER header, not a library one

It ships inside Cavium's own gcc 4.7 tree
(`tools-gcc-4.7/mipsisa64-octeon-elf/include/`), so building with any other
compiler cannot see it, and `cvmx-app-init-linux.c` and `octeon-model.c` both
fail on it. Those two are not optional — between them they define `main()`,
`cvmx_user_app_init()` and the runtime model checks. The build stages that one
file into the SDK copy rather than adding the gcc directory to `-I`, because
that directory also holds a 2012 `stdint.h` and putting it ahead of musl's is a
far larger change than the one file needed.

### A count is not a check

The executive compile loop sends errors to `/dev/null` and carries on, because
five executive files genuinely do not build against musl and none is reachable
(`cvmx-error-trees`, `cvmx-interrupt`, `cvmx-otrace`, `cvmx-tlb`,
`octeon-pci-console`). That tolerance hid a real failure: the two files above
were failing too, the object count still cleared its threshold of 130, and the
build ran on to die at link time on

```
undefined reference to `main'
undefined reference to `cvmx_user_app_init'
```

which points at FFN's own sources rather than at the missing SDK header that
actually caused it. A threshold cannot catch that; the build now **names** the
objects it must have.

## Three prerequisites on the DP, each of which fails silently

| need | absent means |
|---|---|
| `ffn_octeon_info.ko` → `/proc/octeon_info` | `exit(-1)` inside `cvmx_user_app_init()`, before any FFN code runs |
| `ffn_xkphys.ko` | `SIGSEGV` on the first CSR access |
| `/dev/shm` (tmpfs) | `CVMX_SHARED` cannot map; the fork barrier never completes and all 40 cores spin at 100% with no output |

Both modules build against the DP's **running** kernel tree —
`/mnt/clones/fwdport/linux-6.18.49-dp`, which reports
`6.18.49-00007-g616977ba1b11`, matching `uname -r` on the DP — with the same
OpenWrt gcc 13.3.0 that built the kernel (`CONFIG_CC_VERSION_TEXT` says so).
`CONFIG_MODULES=y`, no `MODVERSIONS`, no `MODULE_SIG`, so they load as built.
Build only these two: `ffn_bcm`, `ffn_bde` and `ffn_mdio` are CP and BCM
drivers and have no business being compiled against the DP kernel.

`/proc/octeon_info` then publishes what the SDK runtime parses:

```
dram_size: 32768        eclock_hz: 1600000000
board_type: 20020       dclock_hz:  800000000
```

### The sysmips warning is expected, not a fault

```
sysmips(MIPS_CAVIUM_XKPHYS_WRITE) failed.
  Did you configure your kernel with both:
     CONFIG_CAVIUM_OCTEON_USER_MEM_PER_PROCESS *and*
     CONFIG_CAVIUM_OCTEON_USER_IO_PER_PROCESS?: Invalid argument
```

That is the SDK trying Cavium's own vendor syscall, which upstream 6.18 does not
have. `ffn_xkphys.ko` is the replacement and has already granted the mapping —
which the run proves, because every CSR read after it returned real interface
modes. Treating this as the failure wastes the afternoon it looks like it
deserves.

**`ffn_xkphys.ko` is for the DP only.** It must not be loaded on the CP.

## Deploying it

The DP roots over NFS, so this is a `cp` — see
[`DP-NFSROOT.md`](DP-NFSROOT.md). `dpfwd/deploy-octeon-app-nfs.sh` writes into
`/opt/ffn-cproot-owrt/opt/dproot` on the MP and the file is on the DP when the
write returns.

## Never run it in the foreground of ffn-dpsh

`ffn-dpsh` is ONE shared `/bin/sh` for the whole machine and the only way in. A
forwarder that hangs — the documented outcome when `CVMX_SHARED` places nothing
— would take the control channel with it and leave a DP reset as the only
recovery. `dp-forwarder-run.sh` starts it detached, polls for exit, prints what
it wrote and kills it past a bound, so the shell stays free throughout.

## One core prints, not forty

`cvmx_user_app_init()` forks `appmain()` onto all 40 cores, so anything that
prints does so 40 times — and the lines interleave *mid-table* between cores, so
the columns stop lining up with the interface they describe. The first probe
produced **29628 bytes** of shuffled rows; gated behind `cvmx3_is_init_core()`
it produces **1041** and is readable. The wrapper lives in the octeon3 backend
so `ffn_dp_octeon_main.c` stays free of CVMX headers, as it is for everything
else in that backend.

## What is still missing

**The forwarder does not read `dp.env`.** `ffn_dp_octeon_main.c` takes its ports
from `-p` on the command line and never calls `dp_l3_config_apply()`, so the
config chain still terminates one step short: the MP renders it, the CP relays
it, the DP holds it at `/etc/ffn/dp.env` (30 keys, 6 of them `dp.l3.*`) and the
forwarder ignores it. `dp_l3_config_apply()` and the v6 equivalent are both
linked into the binary and both have unit coverage; what is absent is the call.
