#!/bin/sh
# Run inside the sid-host chroot after downloading the pinned Debian sources
# and extracting their .dsc files with dpkg-source -x. See ADVANCED-ROUTING.md.
set -eu
export LC_ALL=C.UTF-8 LANG=C.UTF-8
cd /build/ffn-router
for spec in json-c-0.19+ds c-ares-1.34.8 libyang-3.13.6; do
 cmake -S "$spec" -B "build-$spec" -DCMAKE_SYSTEM_NAME=Linux -DCMAKE_SYSTEM_PROCESSOR=mips64 -DCMAKE_C_COMPILER=mips64-linux-gnuabi64-gcc -DCMAKE_INSTALL_PREFIX=/usr/local/ffn-router -DCMAKE_INSTALL_LIBDIR=lib -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF -DENABLE_TESTS=OFF -DENABLE_TOOLS=OFF -DENABLE_LYD_PRIV=ON -DPCRE2_LIBRARY=/usr/lib/mips64-linux-gnuabi64/libpcre2-8.so -DPCRE2_INCLUDE_DIR=/usr/include
 cmake --build "build-$spec" -j6
 cmake --install "build-$spec"
done
