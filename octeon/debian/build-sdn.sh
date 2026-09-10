#!/bin/sh
# Run INSIDE sid-host build chroot. Does not deploy or start services.
set -eu
export LC_ALL=C.UTF-8 LANG=C.UTF-8
BASE=${BASE:-/build/ffn-virtual}
mkdir -p "$BASE"
cd "$BASE"
fetch() { test -f "$1" || curl -fL "$2" -o "$1"; }
fetch openvswitch-3.7.1.tar.gz https://www.openvswitch.org/releases/openvswitch-3.7.1.tar.gz
fetch ovn-26.03.2.tar.gz https://github.com/ovn-org/ovn/archive/refs/tags/v26.03.2.tar.gz
printf '%s\n' \
 'b8936c2e95a024d37123536ca843648bc2f1d2520921f991dd3d06248859b70f  openvswitch-3.7.1.tar.gz' \
 '9eebd68e12ced5a14e8adecba0ebd5f3c07867fab89e0c48752f71f043bc1eff  ovn-26.03.2.tar.gz' | sha256sum -c -
test -d openvswitch-3.7.1 || tar xf openvswitch-3.7.1.tar.gz
test -d ovn-26.03.2 || tar xf ovn-26.03.2.tar.gz
cd "$BASE/openvswitch-3.7.1"
./configure --host=mips64-linux-gnuabi64 --build=x86_64-linux-gnu \
 --prefix=/usr/local --localstatedir=/var --sysconfdir=/etc \
 --disable-ssl --disable-libcapng --without-dpdk
make -j6
make DESTDIR="$BASE/ovs-install" install
cd "$BASE/ovn-26.03.2"
./boot.sh
./configure --host=mips64-linux-gnuabi64 --build=x86_64-linux-gnu \
 --prefix=/usr/local --localstatedir=/var --sysconfdir=/etc --disable-ssl \
 --with-ovs-source="$BASE/openvswitch-3.7.1" --with-ovs-build="$BASE/openvswitch-3.7.1"
make -j6
make DESTDIR="$BASE/ovn-install" install
