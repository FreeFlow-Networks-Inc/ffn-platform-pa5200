#!/bin/sh
# Run on the VM after build-plane-kernels.sh. Helpers live beside this script;
# ffn_init.c and ffn-nfsroot-dp.sh come from octeon/initramfs in the repository.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BASE=${BASE:-/mnt/clones/fwdport}
OUT=${OUT:-$BASE/debian-candidates}
PLANES=${PLANES:-"cp dp"}
DP_EXPORT=${DP_EXPORT:-/opt/dproot}
case "$PLANES" in cp|dp|"cp dp") ;; *) echo 'PLANES must be cp, dp, or cp dp' >&2; exit 2;; esac
case "$DP_EXPORT" in /opt/dproot|/opt/dproot-*) ;; *) echo 'unsupported DP export' >&2; exit 2;; esac
case "$DP_EXPORT" in *[!a-zA-Z0-9/_-]*) echo 'invalid DP export characters' >&2; exit 2;; esac
INIT_SOURCE=${INIT_SOURCE:-$HERE/../initramfs}
DP_CONFIG=${DP_CONFIG:-}
CROSS=$BASE/gcc-14.4.0-nolibc/mips64-linux/bin/mips64-linux-
SDK=/mnt/clones/sdk51/OCTEON-SDK/tools-gcc-4.7/bin/mips64-octeon-linux-gnu-gcc
CHROOT=/mnt/clones/debian-mips64/sid-host
exec 9>"$OUT/build.lock"
flock -n 9 || exit 1
sudo install -m644 "$HERE/ffn-systemd-handoff.c" "$CHROOT/tmp/ffn-systemd-handoff.c"
sudo chroot "$CHROOT" mips64-linux-gnuabi64-gcc -O2 -Wall -Wextra -static \
    -o /tmp/ffn-systemd-handoff /tmp/ffn-systemd-handoff.c
for plane in $PLANES; do
    root=$OUT/initramfs-$plane-systemd
    source=$BASE/rootfs/tree
    [ "$plane" = cp ] || source=/mnt/clones/initramfs
    test -d "$root" || sudo cp -a "$source" "$root"
    sudo install -m755 "$CHROOT/tmp/ffn-systemd-handoff" "$root/sbin/ffn-systemd-handoff"
    if [ "$plane" = cp ]; then
        sudo install -m755 "$HERE/ffn-cp-debian-init.sh" "$root/sbin/ffn-cp-debian-init.sh"
        sudo python3 - "$source/init" "$root/init" <<'PY'
from pathlib import Path
import sys
s = Path(sys.argv[1]).read_text()
needle = 'while : ; do\n\t# ffn-nfsroot.sh'
assert s.count(needle) == 1
Path(sys.argv[2]).write_text(s.replace(needle, 'exec /sbin/ffn-cp-debian-init.sh\n\n' + needle))
PY
    else
        # Freestanding _start has no CRT to initialize $gp: disable small data.
        "$SDK" -O2 -G0 -nostdlib -static -fno-builtin -fno-stack-protector \
            -mno-abicalls -fno-pic -mabi=64 -march=octeon3 -msoft-float \
            -Wl,-e,_start -o "$OUT/init-dp-systemd" "$INIT_SOURCE/ffn_init.c"
        sudo install -m755 "$OUT/init-dp-systemd" "$root/init"
        sudo install -m755 "$INIT_SOURCE/ffn-nfsroot-dp.sh" "$root/sbin/ffn-nfsroot"
        sudo sed -i "s|^EXPORT=/opt/dproot$|EXPORT=$DP_EXPORT|" "$root/sbin/ffn-nfsroot"
        test -x "$root/sbin/ffn-systemd-handoff"
        grep -qx "EXPORT=$DP_EXPORT" "$root/sbin/ffn-nfsroot"
        strings "$root/init" | grep -q 'handing PID 1 to Debian systemd'
    fi
    sudo sh -c 'cd "$1" && find . -print0 | cpio --null -o -H newc' sh "$root" > "$OUT/initramfs-$plane-systemd.cpio"
    cd "$OUT/linux-$plane"
    if [ "$plane" = dp ] && [ -n "$DP_CONFIG" ]; then
        test -r "$DP_CONFIG"
        cp "$DP_CONFIG" .config
    fi
    grep -qx CONFIG_CGROUPS=y .config
    scripts/config --set-str INITRAMFS_SOURCE "$OUT/initramfs-$plane-systemd.cpio"
    make ARCH=mips CROSS_COMPILE="$CROSS" olddefconfig
    make -j8 ARCH=mips CROSS_COMPILE="$CROSS" vmlinux modules
    make ARCH=mips CROSS_COMPILE="$CROSS" INSTALL_MOD_PATH="$OUT/modules-$plane-systemd" modules_install
    "${CROSS}strip" -o "$OUT/ffn-vmlinux-systemd-$plane" vmlinux
    sha256sum "$OUT/ffn-vmlinux-systemd-$plane"
done
