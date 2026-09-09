#!/bin/sh
set -eu
BASE=/mnt/clones/debian-mips64
CHROOT=$BASE/sid-host
export LC_ALL=C.UTF-8 LANG=C.UTF-8
mkdir -p "$BASE/images"
case "${1:-both}" in
    base|buildd) kinds=$1 ;;
    both) kinds='base buildd' ;;
    *) echo 'usage: assemble-images.sh [base|buildd|both]' >&2; exit 2 ;;
esac
for kind in $kinds; do
    archive="$CHROOT/tmp/ffn-images/debian-sid-mips64-be-$kind.tar"
    if test ! -f "$archive"; then
        chroot "$CHROOT" sh /root/rebootstrap/build-rootfs.sh "$kind"
    fi
    verified="$BASE/images/verified-$kind.sha256"
    if test -f "$verified" && test -d "$BASE/images/rootfs-$kind" && sha256sum -c --status "$verified"; then
        echo "$kind image already verified"
        continue
    fi
    root=$(mktemp -d "$BASE/images/.verify-$kind-XXXXXXXX")
    tar -xpf "$archive" -C "$root"
    python3 "$BASE/verify-rootfs.py" "$root" > "$BASE/images/verify-$kind.log" 2>&1
    cat "$BASE/images/verify-$kind.log"
    final="$BASE/images/rootfs-$kind"
    if test -e "$final"; then mv "$final" "$final.previous-$(date -u +%Y%m%d-%H%M%S)"; fi
    mv "$root" "$final"
    cp "$archive" "$BASE/images/"
    (cd "$BASE/images" && sha256sum "debian-sid-mips64-be-$kind.tar" > "debian-sid-mips64-be-$kind.tar.sha256")
    sha256sum "$archive" > "$verified"
done
