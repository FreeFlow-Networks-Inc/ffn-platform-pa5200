#!/bin/sh
# Build the staged packet initializer against the operator's configured kernel.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
KDIR=${KDIR:?set configured kernel build directory}
CROSS_COMPILE=${CROSS_COMPILE:?set MIPS64 big-endian compiler prefix}
OUT=${OUT:?set isolated module output directory}
test -s "$KDIR/Module.symvers"
mkdir -p "$OUT"
cp "$HERE/../kctl/ffn_dp_packet_init.c" "$OUT/ffn_dp_packet_init.c"
cp "$HERE/../kctl/ffn_dp_dma.h" "$OUT/ffn_dp_dma.h"
cp "$HERE/../kctl/ffn_dp_trunk.h" "$OUT/ffn_dp_trunk.h"
cp "$HERE/../kctl/ffn_dp_packet_probe.c" "$OUT/ffn_dp_packet_probe.c"
printf 'obj-m += ffn_dp_packet_init.o ffn_dp_packet_probe.o\n' > "$OUT/Makefile"
make -C "$KDIR" M="$OUT" ARCH=mips CROSS_COMPILE="$CROSS_COMPILE" KCFLAGS=-Werror modules
file "$OUT/ffn_dp_packet_init.ko" | grep -q 'MSB.*MIPS'
sha256sum "$OUT/ffn_dp_packet_init.ko"
