#!/bin/sh
# Build the optional read-only PA-5220 DP link probe for an explicit kernel.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
KDIR=${KDIR:?set KDIR to the configured target kernel build}
CROSS_COMPILE=${CROSS_COMPILE:?set the MIPS64 big-endian compiler prefix}
OUT=${OUT:?set an isolated module output directory}
test -s "$KDIR/Module.symvers"
test -s "$KDIR/include/config/kernel.release"
mkdir -p "$OUT"
cp "$HERE/../kctl/ffn_dp_link.c" "$OUT/ffn_dp_link.c"
printf 'obj-m += ffn_dp_link.o\n' > "$OUT/Makefile"
make -C "$KDIR" M="$OUT" ARCH=mips CROSS_COMPILE="$CROSS_COMPILE" KCFLAGS=-Werror modules
file "$OUT/ffn_dp_link.ko" | grep -q 'MSB.*MIPS'
sha256sum "$OUT/ffn_dp_link.ko"
