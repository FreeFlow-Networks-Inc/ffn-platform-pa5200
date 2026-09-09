#!/bin/sh
# Build CPython for mips64 BE from PRISTINE UPSTREAM source, in the
# native-under-qemu build root. The deliverable is a working python3 for the
# CP, not a .deb.
#
# WHY NOT THE DEBIAN PACKAGE. src:python3.14's Build-Depends are a wall for a
# fresh port, and the expensive ones are hard-required rather than profiled:
#
#   locales-all           a glibc REBUILD
#   tk-dev                tcl + tk + the X11 stack
#   systemtap-sdt-dev     src:systemtap
#   python3:any           chicken-and-egg -- python needs python to build
#   net-tools, time, sharutils, valgrind-if-available, media-types
#
# Upstream needs only OpenSSL, zlib, ffi, readline, sqlite3, expat, bz2, lzma,
# ncurses, gdbm and uuid -- and ALL SIXTEEN of those are already built in this
# port (checked with apt-cache policy before starting, not assumed). So this
# reaches a working interpreter in one build instead of a chain. Same trade
# that worked for OpenSSH 10.5p1.
#
# WHY THE .orig TARBALL AND NOT `apt-get source`. apt-get source APPLIES
# Debian's patch series, and those touch configure.ac -- which makes upstream's
# shipped `configure` older than its input, so it refuses to run:
#
#     configure: error: ./configure.ac newer than configure, run autoreconf
#
# For OpenSSH the answer was autoreconf; CPython pins specific autoconf and
# aclocal versions and regenerating with the wrong ones fails in less obvious
# ways. Extracting only the pristine .orig.tar.xz sidesteps the whole question:
# upstream's own generated configure is then newer than upstream's configure.ac
# and simply works.
#
# WHY 3.14. It matches Debian sid, so the root stays internally consistent, and
# the FFN agents that must run on it (ffn_bcmd.py, ffn_cfgagent.py, ffnrun.py)
# import only argparse/json/os/re/select/signal/socket/subprocess/sys/
# threading/time -- nothing removed in 3.12-3.14 -- while using match/case,
# which needs >= 3.10. Checked, rather than assumed from "latest is fine".
set -u

B=/mnt/clones/debian-mips64
T=$B/buildroot
R=$B/sid-host/tmp/repo
OUT=$B/py-build
PORT=8899
VER=${PYVER:-3.14}
LOG=$B/logs/build-cpython.log

# The repo is served over HTTP because apt runs INSIDE the chroot, where a
# host file:// path does not resolve.
#
# pkill -f is in a SCRIPT and not in an ad-hoc command line on purpose: a
# pkill whose pattern also appears in the calling shell's argv kills the
# caller. That happened three times today, twice ending the ssh session and
# once silently skipping the server start so apt then reported the repo
# unreachable. A script's argv is just "sh <path>", so there is nothing to
# match.
pkill -f "http\.server" 2>/dev/null
( cd "$R" && nohup python3 -m http.server "$PORT" --bind 127.0.0.1 \
    >/tmp/repohttp.log 2>&1 & )
sleep 2
if ! curl -sf -o /dev/null "http://127.0.0.1:$PORT/dists/rebootstrap/Release"; then
	echo "repo HTTP server did not come up on $PORT" >&2
	exit 1
fi
echo "repo server up"

for m in proc sys dev; do
	sudo mkdir -p "$T/$m"
	mountpoint -q "$T/$m" || sudo mount --rbind "/$m" "$T/$m"
done
sudo cp /etc/resolv.conf "$T/etc/resolv.conf" 2>/dev/null
sudo mkdir -p "$T/build" "$B/logs"

ch() {
	sudo chroot "$T" /usr/bin/env -i \
		LC_ALL=C.UTF-8 LANG=C.UTF-8 \
		PATH=/usr/sbin:/usr/bin:/sbin:/bin \
		HOME=/root TERM=dumb DEBIAN_FRONTEND=noninteractive \
		"$@"
}

# A half-installed package poisons every later apt run, and the error it
# produces names the WRONG THING. One iproute2 left in state "iU" by a
# `dpkg -i` (dpkg fetches no dependencies, so it could not configure without
# libcap2-bin) made this script fail with
#
#     libexpat1-dev : Depends: libexpat1 (= 2.8.4-1) but it is not going to be
#                     installed
#
# and five more like it -- none of which had anything wrong with them. apt was
# refusing to do ANY work while the tree was inconsistent. Check for it first
# and repair, so the real message is not buried under a cascade.
echo "=== dpkg state (a half-configured package blocks everything) ==="
if [ -n "$(ch dpkg --audit 2>/dev/null)" ]; then
	echo "  inconsistent -- repairing with apt-get -f install"
	ch sh -c "apt-get -f install -y >/build/fix.log 2>&1"
	if [ -n "$(ch dpkg --audit 2>/dev/null)" ]; then
		echo "  STILL inconsistent after repair:"
		ch dpkg --audit 2>&1 | head -6 | sed 's/^/    /'
		exit 1
	fi
	echo "  repaired"
else
	echo "  clean"
fi

echo "=== the 16 libraries upstream actually needs ==="
# The redirect goes INSIDE the chroot (ch sh -c "... >/build/log"), not outside
# it. An outer `ch cmd >"$T/build/log"` is evaluated by the CALLING shell,
# which runs as the unprivileged user, so it fails with "Permission denied" on
# the root-owned build dir -- and does so before the command it is supposed to
# be logging ever runs.
ch sh -c "apt-get install -y --no-install-recommends \
	zlib1g-dev libssl-dev libffi-dev libreadline-dev libsqlite3-dev \
	libexpat1-dev libbz2-dev liblzma-dev libncurses-dev libgdbm-dev \
	uuid-dev pkgconf make gcc libc6-dev \
	>/build/pydeps.log 2>&1"
if [ $? != 0 ]; then
	echo "  FAILED to install build libraries"
	sudo grep -aE "^E:|but it is" "$T/build/pydeps.log" | head -6 | sed 's/^/    /'
	exit 1
fi
echo "  installed"

echo "=== fetch the source, pristine upstream only ==="
# -d = download only. We then unpack ONLY the .orig tarball.
ch sh -c "cd /build && apt-get source -d python$VER >/dev/null 2>&1" || {
	echo "  FAILED to download source"; exit 1; }
ORIG=$(sudo sh -c "ls $T/build/python$VER*.orig.tar.xz 2>/dev/null | head -1")
if [ -z "$ORIG" ]; then
	echo "  no .orig.tar.xz found; got:"
	sudo sh -c "ls $T/build/ 2>/dev/null" | sed 's/^/    /'
	exit 1
fi
echo "  $(basename "$ORIG")"
sudo rm -rf "$T/build/cpython"
sudo mkdir -p "$T/build/cpython"
ch sh -c "cd /build/cpython && tar -xJf /build/$(basename "$ORIG") --strip-components=1" || {
	echo "  FAILED to unpack"; exit 1; }
echo "  unpacked: $(sudo sh -c "cat $T/build/cpython/Include/patchlevel.h 2>/dev/null" | sed -n 's/.*PY_VERSION *"\([^"]*\)".*/\1/p')"

echo "=== configure ==="
# --with-ensurepip=no  : no wheels to fetch, and pip is not what the CP needs.
# --disable-test-modules: the test extension modules are a large slice of the
#                        build and nothing on the CP imports them.
# NOT --enable-optimizations: PGO runs a profile task with the freshly built
#                        interpreter, which under emulation costs more than
#                        the speed is worth here.
# NOT --enable-shared  : a static libpython inside the binary avoids RPATH and
#                        loader-order questions in a root that is served over
#                        NFS.
ch sh -c "cd /build/cpython && ./configure \
	--prefix=/usr \
	--with-ensurepip=no \
	--disable-test-modules \
	--with-system-expat \
	--with-system-ffi \
	>/build/pyconfigure.log 2>&1"
rc=$?
echo "  rc=$rc"
if [ "$rc" != 0 ]; then
	sudo tail -25 "$T/build/pyconfigure.log" | sed 's/^/    /'
	exit 1
fi
sudo grep -aE "^checking for (build|host)|^checking whether.*big.endian|MACHDEP" \
	"$T/build/pyconfigure.log" 2>/dev/null | head -6 | sed 's/^/    /'

echo "=== make (long: every .c under emulation, then the stdlib byte-compile) ==="
ch sh -c "cd /build/cpython && make -j8 >/build/pymake.log 2>&1"
rc=$?
echo "  rc=$rc"
sudo cp "$T/build/pymake.log" "$LOG" 2>/dev/null
if [ "$rc" != 0 ]; then
	echo "  --- errors (-a: build logs carry control chars and plain grep hides them) ---"
	sudo grep -anE "internal compiler error|error:|Error [0-9]|undefined reference|cannot find" \
		"$T/build/pymake.log" | tail -15 | sed 's/^/    /'
	exit 1
fi

echo "=== what came out, and does it run? ==="
sudo sh -c "ls -l $T/build/cpython/python 2>/dev/null" | sed 's/^/  /'
sudo sh -c "file -b $T/build/cpython/python 2>/dev/null" | cut -c1-72 | sed 's/^/  /'
ch sh -c "cd /build/cpython && ./python -c \"
import sys, sysconfig, struct, ssl, sqlite3, zlib, lzma, bz2, ctypes, json, select, socket, subprocess, threading
print('version   ', sys.version.split()[0])
print('platform  ', sysconfig.get_platform())
print('byteorder ', sys.byteorder)
print('maxsize   ', sys.maxsize)
print('ssl       ', ssl.OPENSSL_VERSION)
print('sqlite3   ', sqlite3.sqlite_version)
print('modules   ', 'zlib lzma bz2 ctypes json select socket subprocess threading all import')
\"" 2>&1 | sed 's/^/  /'

echo "=== the modules that failed to build (expect none that matter) ==="
sudo grep -aA12 "necessary bits to build these modules" "$T/build/pymake.log" 2>/dev/null \
	| head -14 | sed 's/^/  /'

echo "=== install into a staging dir (not the root yet) ==="
sudo rm -rf "$T/build/pyinstall"
ch sh -c "cd /build/cpython && make install DESTDIR=/build/pyinstall >/build/pyinstall.log 2>&1"
rc=$?
echo "  make install rc=$rc"
if [ "$rc" != 0 ]; then
	sudo tail -15 "$T/build/pyinstall.log" | sed 's/^/    /'
	exit 1
fi
sudo mkdir -p "$OUT"
sudo sh -c "rm -rf $OUT/* 2>/dev/null; cp -a $T/build/pyinstall/. $OUT/"
echo "  staged in $OUT ($(sudo du -sh $OUT 2>/dev/null | cut -f1))"
sudo sh -c "ls $OUT/usr/bin/python* 2>/dev/null" | sed 's/^/    /'
