#!/bin/sh
# Install the completion helpers into the existing Ubuntu build VM.
set -eu
test "$(id -u)" = 0 || { echo 'Run with sudo' >&2; exit 1; }
BASE=/mnt/clones/debian-mips64
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CHROOT=$BASE/sid-host
test -f "$CHROOT/root/rebootstrap/bootstrap.sh"
for f in ensure-chroot-mounts.sh prepare-mips64-emulation.py install-port-extension.py complete-port.sh resume-port.sh build-rootfs.sh verify-rootfs.py assemble-images.sh fix-apt-metadata.py; do
    if test "$HERE" != "$BASE"; then install -m 755 "$HERE/$f" "$BASE/$f"; fi
done
sh "$BASE/ensure-chroot-mounts.sh" "$CHROOT"
chroot "$CHROOT" /usr/bin/env LC_ALL=C.UTF-8 DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends qemu-user mmdebstrap mount
python3 "$BASE/prepare-mips64-emulation.py"
install -m 755 "$BASE/complete-port.sh" "$BASE/build-rootfs.sh" "$BASE/fix-apt-metadata.py" "$CHROOT/root/rebootstrap/"
python3 "$BASE/install-port-extension.py"
sh -n "$CHROOT/root/rebootstrap/bootstrap.sh"
echo 'Ready: systemd-run --unit=ffn-debian-port --collect /bin/sh /mnt/clones/debian-mips64/resume-port.sh'
