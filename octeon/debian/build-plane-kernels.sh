#!/bin/sh
# Build isolated CP/DP candidates; never replace the running/staged kernels.
set -eu
BASE=${BASE:-/mnt/clones/fwdport}
OUT=${OUT:-$BASE/debian-candidates}
CROSS=$BASE/gcc-14.4.0-nolibc/mips64-linux/bin/mips64-linux-
mkdir -p "$OUT"
exec 9>"$OUT/build.lock"
flock -n 9 || { echo 'candidate kernel build already active' >&2; exit 1; }
for plane in cp dp; do
    src=$BASE/linux-6.18.49
    [ "$plane" = cp ] || src=$BASE/linux-6.18.49-dp
    tree=$OUT/linux-$plane
    if [ ! -d "$tree" ]; then cp -a --reflink=auto "$src" "$tree"; fi
    cd "$tree"
    # Clause 45's read frame carries DEVAD, not the register number.
    # See 0013-mdio-cavium-c45-device-address.patch. Keep copied older trees
    # reproducible, while accepting trees where the fix is already present.
    python3 - <<'PY'
from pathlib import Path
p = Path('drivers/net/mdio/mdio-cavium.c')
s = p.read_text()
before, body = s.split('int cavium_mdiobus_read_c45(', 1)
body, after = body.split('EXPORT_SYMBOL(cavium_mdiobus_read_c45);', 1)
old = 'smi_cmd.s.reg_adr = regnum;'
new = 'smi_cmd.s.reg_adr = devad;'
if old in body:
    assert body.count(old) == 1
    body = body.replace(old, new)
assert body.count(new) == 1
p.write_text(before + 'int cavium_mdiobus_read_c45(' + body +
             'EXPORT_SYMBOL(cavium_mdiobus_read_c45);' + after)
PY
    test -f "$OUT/config-$plane.before" || cp .config "$OUT/config-$plane.before"
    # Keep each plane's proven hardware configuration and initramfs. Enable
    # systemd's kernel interfaces, including service isolation and cgroup v2.
    for symbol in CGROUPS CGROUP_PIDS MEMCG CGROUP_SCHED FAIR_GROUP_SCHED \
        CFS_BANDWIDTH CPUSETS NAMESPACES UTS_NS IPC_NS PID_NS NET_NS USER_NS \
        DEVTMPFS INOTIFY_USER SIGNALFD TIMERFD EPOLL UNIX SYSFS PROC_FS \
        FHANDLE SECCOMP SECCOMP_FILTER TMPFS TMPFS_XATTR TMPFS_POSIX_ACL; do
        scripts/config --enable "$symbol"
    done
    scripts/config --disable RT_GROUP_SCHED --disable LOCALVERSION_AUTO \
        --set-str LOCALVERSION "-ffn-debian-$plane"
    # CP serves the DP root. Build nfsd in, so this candidate cannot load a
    # stale nfsd.ko from the previous kernel's NFS root.
    [ "$plane" != cp ] || scripts/config --enable NFSD
    make ARCH=mips CROSS_COMPILE="$CROSS" olddefconfig
    for symbol in CGROUPS NAMESPACES NET_NS USER_NS DEVTMPFS FHANDLE; do
        grep -qx "CONFIG_$symbol=y" .config || exit 1
    done
    make -j8 ARCH=mips CROSS_COMPILE="$CROSS" vmlinux modules
    cp .config "$OUT/config-$plane"
    cp vmlinux "$OUT/vmlinux-$plane.debug"
    "${CROSS}strip" -o "$OUT/ffn-vmlinux-debian-$plane" vmlinux
    make ARCH=mips CROSS_COMPILE="$CROSS" INSTALL_MOD_PATH="$OUT/modules-$plane" modules_install
    file "$OUT/ffn-vmlinux-debian-$plane"
    (cd "$OUT" && sha256sum "ffn-vmlinux-debian-$plane" > "ffn-vmlinux-debian-$plane.sha256")
    echo "VERIFIED BUILD: $plane $(make -s ARCH=mips CROSS_COMPILE="$CROSS" kernelrelease)"
done
echo 'CP and DP kernel candidates built; hardware boot tests still required.'
