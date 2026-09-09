#!/bin/sh
set -eu
BASE=${BASE:-/mnt/clones/fwdport/debian-candidates}
TREE=$BASE/linux-cp-management
CROSS=/mnt/clones/fwdport/gcc-14.4.0-nolibc/mips64-linux/bin/mips64-linux-
test -d "$TREE" || cp -a --reflink=auto "$BASE/linux-cp" "$TREE"
cd "$TREE"
scripts/config --module I2C_MUX
scripts/config --keep-case --module I2C_MUX_PCA954x
make ARCH=mips CROSS_COMPILE="$CROSS" olddefconfig
make -j8 ARCH=mips CROSS_COMPILE="$CROSS" modules
test "$(make -s ARCH=mips CROSS_COMPILE="$CROSS" kernelrelease)" = 6.18.49-ffn-debian-cp-dirty
mkdir -p "$BASE/management-modules"
cp drivers/i2c/i2c-mux.ko drivers/i2c/muxes/i2c-mux-pca954x.ko "$BASE/management-modules/"
