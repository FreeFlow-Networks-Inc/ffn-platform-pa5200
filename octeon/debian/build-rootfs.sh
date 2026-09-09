#!/bin/sh
# Run inside the sid-host chroot after complete-port.sh has built its packages.
set -eu
export LC_ALL=C.UTF-8 LANG=C.UTF-8
kind=${1:-base}
case "$kind" in
    base) extra=systemd-sysv,login,mount,kmod,e2fsprogs,util-linux,netbase ;;
    buildd) extra=systemd-sysv,login,mount,kmod,e2fsprogs,util-linux,netbase,build-essential,debhelper,perl ;;
    *) echo 'usage: build-rootfs.sh [base|buildd] [--simulate]' >&2; exit 2 ;;
esac
# Also enumerate the base tools explicitly so --simulate checks the same
# usable package set even before mmdebstrap has extracted Essential packages.
extra="$extra,base-files,base-passwd,bash,coreutils,dash,debianutils,diffutils,dpkg,findutils,grep,gzip,hostname,ncurses-base,ncurses-bin,perl-base,sed,sysvinit-utils,tar,debconf,ca-certificates"
mkdir -p /tmp/ffn-images
output=/tmp/ffn-images/debian-sid-mips64-be-$kind.tar
test ! -e "$output" || { echo "Refusing to overwrite $output" >&2; exit 1; }
partial="$output.partial.$$"
# An empty SUITE selects Essential packages from every listed mirror. Using
# "sid" here excludes our locally built essentials (codename rebootstrap).
mmdebstrap ${2:-} --architectures=mips64 --variant=apt --format=tar \
    --include="$extra" --aptopt='Acquire::Languages "none"' \
    --hook-dir=/usr/share/mmdebstrap/hooks/file-mirror-automount \
    --setup-hook='mkdir -p "$1/usr/lib64"; test -e "$1/lib64" || ln -s usr/lib64 "$1/lib64"' \
    --customize-hook='chroot "$1" dpkg --audit' \
    --customize-hook='chroot "$1" dpkg-query -W > "$1/etc/ffn-package-manifest"' \
    --customize-hook='printf "ffn-mips64\n" > "$1/etc/hostname"' \
    '' "$partial" \
    'deb [trusted=yes arch=mips64,all] file:///tmp/repo rebootstrap main' \
    'deb [arch=all] https://deb.debian.org/debian sid main' \
    'deb-src https://deb.debian.org/debian sid main'
if test -f "$partial"; then
    mv "$partial" "$output"
    sha256sum "$output" > "$output.sha256"
    echo "Created $output"
fi
