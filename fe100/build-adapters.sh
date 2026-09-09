#!/bin/sh
# Run with a MIPS64 big-endian n64 glibc compiler (on the build VM/chroot).
set -eu
src=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
out=${1:-"$src/build"}
cc=${CC:-mips64-linux-gnuabi64-gcc}
mkdir -p "$out"
"$cc" -shared -fPIC -O2 -Wall -Wextra -Werror -pthread \
    -o "$out/libffn-fe100-mmio.so" "$src/ffn_fe100_mmio.c"
"$cc" -DFFN_BLOCK_BASE=0x8000 -shared -fPIC -O2 -Wall -Wextra -Werror -pthread \
    -o "$out/libffn-fe100-tmi-mmio.so" "$src/ffn_fe100_mmio.c"
"$cc" -shared -fPIC -O2 -Wall -Wextra -Werror -pthread \
    -o "$out/libffn-fe100-tables.so" "$src/ffn_fe100_mmio.c"
"$cc" -shared -fPIC -O2 -Wall -Wextra -Werror \
    -o "$out/libffn-gearbox-mdio.so" "$src/ffn_gearbox_mdio.c"
