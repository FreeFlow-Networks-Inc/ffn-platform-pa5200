# A control-plane userland with a package manager

The CP had a kernel and an initramfs, and later an NFS root holding a Buildroot
glibc tree. What it never had was a *package manager*: adding a tool meant a
cross-build on another machine. This gives it one, with ~9,500 prebuilt
packages.

## Why OpenWrt

The CN73XX control plane is **big-endian mips64, n64 ABI**. Almost nothing ships
for that target. Debian has `mips64el` (little-endian) only; Gentoo would work
but is source-only, and compiling on eight 1.2 GHz cores is not a plan.

OpenWrt's `mips64_octeonplus` architecture is built for exactly this CPU. That
was verified against our own binaries before adopting anything, not assumed:

    OpenWrt opkg   ELF64  big endian  mips64r2  octeon    interp /lib/ld-musl-mips64-sf.so.1
    FFN bash       ELF64  big endian  mips64r2  octeon3   interp /lib64/ld.so.1

Same word size, endianness and ISA; `octeon` code runs on `octeon3`. The build
directory name `target-mips64_octeonplus_64_musl` confirms n64 rather than n32,
which matters because n32 would have needed `CONFIG_MIPS32_N32` in the kernel.

Feed sizes at 24.10.4: base 693, packages 4528, luci 3355, plus routing and
telephony -- 9465 visible to `opkg list` once the indexes are in.

## How the tree is built

`ffn-cp-owrt-stage.sh` runs on the MP. The rootfs comes from the official
OpenWrt **ImageBuilder** for `octeon/generic` rather than a downloaded tarball,
because that target publishes no standalone rootfs -- only per-device images --
and because ImageBuilder makes the package set explicit and reproducible.

    make image PROFILE=generic PACKAGES="openssh-server pciutils python3 \
        tcpdump strace gdb ethtool ip-full coreutils ... -dropbear"

Never add a `kmod-*` package. Those are built against OpenWrt's 6.6 kernel and
this CP runs FFN's own 6.18; the staging script comments the `openwrt_kmods`
feed out of `distfeeds.conf` for that reason.

### The tree stays pure musl

glibc contributes exactly two files, `/lib/ld.so.1` and `/lib/libc.so.6`,
because FFN's own CP binaries need exactly two -- `readelf -d ffn_pcnetd` shows
one `NEEDED`, `libc.so.6`, and interpreter `/lib64/ld.so.1`, which resolves
through OpenWrt's `/lib64 -> lib` symlink.

Copying the whole glibc runtime in is a trap, and it bit once. That same
`/lib64 -> lib` symlink means anything aimed at `/lib64` lands in `/lib`, and
`libgcc_s.so.1` is the one soname present in **both** trees -- it is a compiler
runtime, not part of libc, so it is not covered by "glibc and musl use
different names". The glibc build references `_dl_find_object`, which musl does
not provide, so every musl binary in the tree died at relocation:

    Error relocating /lib/libgcc_s.so.1: _dl_find_object: symbol not found

The script now refuses to overwrite any name musl shipped, and exits rather
than continuing.

### Runtime directories

OpenWrt has `/var -> /tmp` and leaves procd to create `/tmp/{lock,run,log,...}`
during preinit. FFN never runs procd -- the initramfs chroots straight into
this root -- so `opkg` failed with

    opkg_conf_load: Could not create lock file /var/lock/opkg.lock

The script creates them in the tree and installs `/sbin/ffn-cp-prepare` to redo
it at entry, for when `/tmp` is a tmpfs.

## Reaching the feed without giving the CP an internet path

The CP's transport address is in `127/8`, so routing it outward would mean
relaxing martian-source handling on a firewall. Instead the MP runs a caching
proxy (`ffn-owrt-mirror.conf`, nginx) bound to `127.1.1.1:8080` only -- nothing
is exposed on the management LAN -- and `distfeeds.conf` points there.

Plain HTTP over that link is deliberate and safe: `opkg.conf` keeps
`option check_signature` and the tree carries the release key `d310c6f2833e97f7`,
so every index is usign-verified end to end. The proxy is a cache, not a trust
boundary. Confirmed in the logs -- `Signature check passed.` on all six feeds.

Only what is actually installed gets stored, so this costs megabytes rather
than mirroring gigabytes, and `.ipk` files are immutable once published, hence
the 180-day cache validity.

## Using it

    bash ffn-cp-owrt-stage.sh                 # on the MP; re-runnable
    cp ffn-owrt-mirror.conf /etc/nginx/conf.d/ && systemctl reload nginx

From the CP:

    /sbin/ffn_nfsmount 127.1.1.1:/opt/ffn-cproot-owrt /tmp/owrt \
        nolock,vers=3,addr=127.1.1.1,proto=tcp,mountproto=tcp,hard
    mount -t proc proc /tmp/owrt/proc; mount -o bind /dev /tmp/owrt/dev
    mount -t sysfs sys /tmp/owrt/sys        # do not skip: see below
    chroot /tmp/owrt /sbin/ffn-cp-prepare
    chroot /tmp/owrt /bin/opkg update
    chroot /tmp/owrt /bin/opkg install <pkg>

Verified on hardware: bash 5.2.37, Python 3.11.14, gdb 15.2, strace 6.12,
tcpdump 4.99.5, curl 8.19.0, setpci 3.14.0, and `jq` installed live from the
feed and round-tripping JSON correctly on big-endian mips64.

`/opt/ffn-cproot` is left exactly as it was. This is a second export, so the
proven boot path is untouched; switching the default means pointing
`ffn-nfsroot.sh` at `/opt/ffn-nfs/cproot-owrt`.

## Two traps worth remembering

**Mount sysfs before trusting a PCI listing.** `ls /sys/bus/pci/devices` in a
chroot whose sysfs never mounted returns stale directories left on the NFS
tree. That produced a confident and wrong conclusion that the CN78XX dataplane
had stopped enumerating under 6.18. `lspci` with sysfs mounted shows it
present: `0003:03:00.0 MIPS [0b30] Cavium Octeon III CN78XX [177d:0095] rev 08`,
alongside the FE100's two functions at `0001:01:00.x` and a PLX PEX 8606 switch
on bus `0003:01`.

**`[ -e ]` lies about OpenWrt symlinks.** Many binaries are absolute symlinks
into `/usr/libexec` (`lspci` -> `/usr/libexec/lspci-pciutils`), which dangle
when tested from outside the chroot. Test by running the binary, not by
stat-ing the path.
