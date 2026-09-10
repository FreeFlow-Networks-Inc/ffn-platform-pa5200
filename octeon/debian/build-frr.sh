#!/bin/sh
# Build inside sid-host after build-frr-deps.sh. Host clippy needs libelf-dev
# and python3-dev. This commissioning build runs daemons as root in lab netns;
# capability-based privilege separation is not configured.
set -eu
export LC_ALL=C.UTF-8 LANG=C.UTF-8
cd /build/ffn-router/frr-10.7.1
./bootstrap.sh
mkdir -p /build/ffn-router/build-frr
cd /build/ffn-router/build-frr
export PKG_CONFIG_LIBDIR=/usr/local/ffn-router/lib/pkgconfig:/usr/lib/mips64-linux-gnuabi64/pkgconfig
export CPPFLAGS=-I/usr/local/ffn-router/include
export LDFLAGS="-L/usr/local/ffn-router/lib -Wl,-rpath,/usr/local/ffn-router/lib"
../frr-10.7.1/configure --host=mips64-linux-gnuabi64 --build=x86_64-linux-gnu --prefix=/usr/local/ffn-router --sysconfdir=/etc --localstatedir=/var --disable-doc --disable-capabilities --disable-snmp --disable-rpki --disable-nhrpd --disable-protobuf --disable-libunwind --disable-zeromq --disable-ospfclient --disable-ospfapi --enable-multipath=64
make -j6
make DESTDIR=/build/ffn-router/frr-install install
