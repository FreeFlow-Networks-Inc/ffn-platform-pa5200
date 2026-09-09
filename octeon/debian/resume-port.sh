#!/bin/sh
set -eu
BASE=/mnt/clones/debian-mips64
exec 9>"$BASE/port-build.lock"
flock -n 9 || { echo 'Port build already running'; exit 1; }
sh "$BASE/ensure-chroot-mounts.sh" "$BASE/sid-host"
python3 "$BASE/prepare-mips64-emulation.py"
log="$BASE/logs/complete-port-$(date -u +%Y%m%d-%H%M%S).log"
echo "$log" > "$BASE/logs/complete-port.latest"
echo running > "$BASE/logs/complete-port.exit"
set +e
chroot "$BASE/sid-host" /usr/bin/env -i LC_ALL=C.UTF-8 LANG=C.UTF-8 PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root SHELL=/bin/sh TERM=dumb /bin/sh -c 'cd /root/rebootstrap && ./bootstrap.sh HOST_ARCH=mips64 ENABLE_MULTIARCH_GCC=no' > "$log" 2>&1
rc=$?
if test "$rc" = 0; then
    sh "$BASE/assemble-images.sh" >> "$log" 2>&1
    rc=$?
fi
echo "$rc" > "$BASE/logs/complete-port.exit"
exit "$rc"
