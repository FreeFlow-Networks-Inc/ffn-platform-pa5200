#!/bin/sh
# Build openssh for mips64 BE from UPSTREAM source, inside the mips64 root.
#
# WHY NOT THE DEBIAN PACKAGE. Its Build-Depends pull a whole documentation and
# language-binding toolchain that does not exist for mips64 yet:
#
#   libfido2   -> cmake, libcbor-dev, libudev-dev
#   libselinux -> ruby, python3, libsepol-dev, libpcre2-dev
#   wtmpdb     -> docbook-xml, docbook-xsl, docbook-xsl-ns
#   dh-runit   -> perl-doc + 3 perl modules
#
# That closure is hundreds of packages. Upstream openssh needs only OpenSSL and
# zlib, both already in the local repo, so it reaches a working sshd in one
# build. The deliverable here is a BINARY sshd for the CP, not a .deb -- the CP
# needs to stay reachable, and that does not require Debian packaging.
#
# Also a real test of a recorded hazard: OpenSSH previously hit an internal
# compiler error on mips64 (GCC PR110934) with the OpenWrt gcc 13 toolchain.
# This builds with the bootstrapped gcc 16, so it says whether that is still
# live.
set -u
B=/mnt/clones/debian-mips64
R=$B/sid-host/tmp/repo
T=$B/buildroot
OUT=$B/ssh-build
PORT=8899

pkill -f "http.server $PORT" 2>/dev/null
( cd "$R" && nohup python3 -m http.server "$PORT" --bind 127.0.0.1 \
    >/tmp/repohttp.log 2>&1 & )
sleep 2

for m in proc sys dev; do
	sudo mkdir -p "$T/$m"
	mountpoint -q "$T/$m" || sudo mount --rbind "/$m" "$T/$m"
done
sudo cp /etc/resolv.conf "$T/etc/resolv.conf" 2>/dev/null

CH="sudo chroot $T /usr/bin/env -i LC_ALL=C.UTF-8 LANG=C.UTF-8 PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root TERM=dumb DEBIAN_FRONTEND=noninteractive"

echo "=== install just what upstream openssh needs ==="
$CH apt-get install -y --no-install-recommends \
	libssl-dev zlib1g-dev libpam0g-dev autoconf automake libtool \
	pkgconf make gcc libc6-dev \
	2>&1 | tail -3

echo "=== fetch the source ==="
sudo mkdir -p "$T/build"
$CH sh -c 'cd /build && apt-get source openssh 2>&1 | tail -3'
SRCDIR=$(sudo sh -c "ls -d $T/build/openssh-* 2>/dev/null | head -1")
if [ -z "$SRCDIR" ]; then
	echo "no source tree unpacked" >&2
	exit 1
fi
echo "  source: ${SRCDIR#$T}"

# The Debian source applies patches that touch configure.ac, so the shipped
# configure is older than its input and openssh refuses to run it:
#   configure: error: ./configure.ac newer than configure, run autoreconf
echo "=== autoreconf (Debian's patches touched configure.ac) ==="
$CH sh -c "cd ${SRCDIR#$T} && autoreconf -fi >/build/autoreconf.log 2>&1"
rc=$?
echo "  autoreconf rc=$rc"
if [ "$rc" != 0 ]; then
	sudo tail -15 "$T/build/autoreconf.log" | sed 's/^/    /'
	exit 1
fi

echo "=== configure (upstream, not debian/rules) ==="
$CH sh -c "cd ${SRCDIR#$T} && ./configure \
	--prefix=/usr --sysconfdir=/etc/ssh \
	--with-privsep-path=/run/sshd \
	--with-pam --with-zlib --with-ssl-engine \
	>/build/configure.log 2>&1"
rc=$?
echo "  configure rc=$rc"
if [ "$rc" != 0 ]; then
	sudo tail -20 "$T/build/configure.log" | sed 's/^/    /'
	exit 1
fi
sudo grep -E "^(OpenSSH has been configured|.*Host:|.*Compiler:|.*PAM support:)" \
	"$T/build/configure.log" 2>/dev/null | head -6 | sed 's/^/    /'

# STRIP -fzero-call-used-regs=used. gcc 16.2.0 on mips64 ICEs on moduli.c with
# it present:
#   moduli.c:769:1: internal compiler error: in int_mode_for_mode,
#                   at stor-layout.cc:408
#
# Bisected against openssh's hardening set rather than guessed -- dropping
# -ftrapv, -ftrivial-auto-var-init or -fno-builtin-memset all still ICE, and
# dropping ONLY -fzero-call-used-regs compiles. moduli.c also builds fine at
# -O0/-O1/-O2 without it, so this is not an optimisation-level problem.
#
# This IS GCC PR110934 -- the same bug the project notes already recorded for
# "OpenSSH mips64 ICE", not a new one. Same ICE function (int_mode_for_mode),
# same file (stor-layout.cc, 407 vs 408 -- source drift between gcc versions),
# same RTL pass (zero_call_used_regs), same triggering flag, same source file.
# The DP's Buildroot gcc 13.4 hit it at moduli.c:814 / stor-layout.cc:407.
#
# The NEWS is that it is STILL LIVE IN GCC 16.2.0. That strengthens the
# upstreamable point recorded against the DP build: Buildroot already models
# this class as BR2_TOOLCHAIN_HAS_GCC_BUG_110934 -> --without-hardening, but
# gates it on "default y if BR2_m68k", so mips64 never trips the guard.
#
# Note openssh's configure probes -fzero-call-used-regs with a TRIVIAL function,
# which compiles fine, so the flag gets enabled and real code breaks the
# compiler hundreds of steps later. That is why this keeps resurfacing.
#
# Everything else in the hardening set is kept. Editing the generated Makefile
# rather than passing CFLAGS= because configure APPENDS its hardening flags to
# CFLAGS, so anything passed in would be overridden by the very flag being
# removed.
echo "=== strip the ICE-triggering flag from the generated Makefile ==="
$CH sh -c "cd ${SRCDIR#$T} && sed -i 's/ -fzero-call-used-regs=used//g' Makefile"
$CH sh -c "cd ${SRCDIR#$T} && grep -c 'fzero-call-used-regs' Makefile" 	| sed 's/^/  occurrences remaining: /'

echo "=== make (this is the long part under emulation) ==="
$CH sh -c "cd ${SRCDIR#$T} && make -j4 >/build/make.log 2>&1"
rc=$?
echo "  make rc=$rc"
if [ "$rc" != 0 ]; then
	echo "  --- last 25 lines ---"
	sudo tail -25 "$T/build/make.log" | sed 's/^/    /'
	exit 1
fi

echo "=== what came out ==="
for b in sshd ssh ssh-keygen scp sftp-server sshd-session sshd-auth; do
	f=$(sudo find "$SRCDIR" -maxdepth 2 -name "$b" -type f 2>/dev/null | head -1)
	if [ -n "$f" ]; then
		printf '  %-12s %s\n' "$b" "$(sudo file -b "$f" | cut -c1-72)"
	else
		printf '  %-12s NOT BUILT\n' "$b"
	fi
done
sudo mkdir -p "$OUT"
for b in sshd ssh ssh-keygen scp sftp-server sshd-session sshd-auth; do
	# A successful sshd -t does not prove that a connection can authenticate:
	# modern OpenSSH execs these separate session/authentication helpers.
	sudo install -m755 "$SRCDIR/$b" "$OUT/$b" || exit 1
done
echo "  copied to ${OUT}"
