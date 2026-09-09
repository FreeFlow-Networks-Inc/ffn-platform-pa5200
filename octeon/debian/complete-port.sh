#!/bin/sh
# Sourced by the patched rebootstrap after its cross bootstrap stages.
# QEMU executes configuration probes; all compiler invocations still run on amd64.
patch_perl() {
    # Probe the actual BE target ABI instead of borrowing mips64el config data.
    drop_privs python3 -c 'from pathlib import Path; p=Path("debian/config.debian"); s=p.read_text(); old="if [ \"$build_gnu_type\" = \"$host_gnu_type\" ]"; assert old in s; p.write_text(s.replace(old, "if [ \"$host_arch\" = mips64 ] || " + old[3:], 1))'
}
buildenv_perl() {
    export LC_ALL=C LANG=C
    export DEB_BUILD_OPTIONS='nocheck noddebs parallel=8'
}
cross_build perl
mark_built perl
echo 'FFN: Perl target packages complete'

if test ! -f "$REPODIR/stamps/apt_gpgv"; then
    python3 /root/rebootstrap/fix-apt-metadata.py
    $APT_GET update
    touch "$REPODIR/stamps/apt_gpgv"
fi
echo 'FFN: APT with gpgv verification complete'

if test ! -f /tmp/ffn-images/debian-sid-mips64-be-base.tar; then
    sh /root/rebootstrap/build-rootfs.sh base
fi
echo 'FFN: base root filesystem assembled'

if test -f "$REPODIR/stamps/gcc_native"; then
    echo 'FFN: native GCC already built'
else
    apt_get_install libgmp-dev:mips64 libmpfr-dev:mips64 libmpc-dev:mips64 \
        libisl-dev:mips64 libzstd-dev:mips64 zlib1g-dev:mips64 \
        libstdc++-16-dev-mips64-cross g++-16-mips64-linux-gnuabi64 \
        autoconf2.69 automake libtool bison flex gperf texinfo sharutils \
        patchutils quilt chrpath lsb-release time pkgconf libzstd-dev
    cross_build_setup gcc-16 gcc_native
    (
        # Debian's cross-build-native mode produces MIPS64 executables using
        # the amd64 cross compiler. Keep the top-level make serial.
        export DEB_BUILD_OPTIONS='nocheck noddebs parallel=8 nostrap nolto nopgo nolang=ada,algol68,asan,biarch,brig,cobol,d,gcn,go,itm,java,jit,hppa64,lsan,m2,nvptx,objc,obj-c++,rust,tsan,ubsan,fortran'
        export WITH_BOOTSTRAP=off
        drop_privs dpkg-buildpackage -a mips64 -Pcross,nocheck,nobiarch -d -T control
        # Full default build-deps include languages deliberately disabled above.
        # The selected language/library prerequisites are installed explicitly.
        drop_privs dpkg-buildpackage -a mips64 -Pcross,nocheck,nobiarch -d -B -uc -us
    )
    cd ..
    # Preserve the amd64 cross compiler from the same gcc-16 source package.
    # pickup_packages removes every architecture of a source, which is too
    # broad when adding a native compiler alongside its cross compiler.
    archive="$REPODIR/archive/gcc-native-$(date -u +%Y%m%d-%H%M%S)"
    mkdir -p "$archive"
    for incoming in ./*_mips64.deb; do
        pkgname=$(dpkg-deb -f "$incoming" Package)
        for old in $(reprepro --list-format '${Filename}\n' listfilter rebootstrap "Package (== $pkgname), Architecture (== mips64)"); do
            cp "$REPODIR/$old" "$archive/"
        done
        reprepro -A mips64 remove rebootstrap "$pkgname"
    done
    pickup_additional_packages *.changes
    touch "$REPODIR/stamps/gcc_native"
    cd /root/rebootstrap
fi
echo 'FFN: native GCC target packages complete'
