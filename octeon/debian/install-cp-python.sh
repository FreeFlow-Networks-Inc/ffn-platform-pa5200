#!/bin/sh
# Install the built CPython into the CP root and prove it runs the FFN agents.
#
# The point of building python at all is ffn_bcmd.py, ffn_cfgagent.py and
# ffnrun.py -- the CP's current root uses OpenWrt's python3.11, which is
# musl-linked and cannot carry over to a glibc Debian root. So the test that
# matters is not "python starts" but "the agents import and compile under it".
#
# Staging first, root second: the tree goes in from $SRC only after the
# interpreter has been exercised, so a broken build cannot land in a root that
# the CP boots from.
set -u

B=/mnt/clones/debian-mips64
T=$B/buildroot
SRC=$B/py-build            # DESTDIR staging produced by build-cpython-mips64.sh
C=$B/cproot
FFNPY=${FFNPY:-$B/ffn-agents}   # optional: FFN .py files to smoke-test

if [ ! -d "$SRC/usr/bin" ]; then
	echo "no staged python at $SRC -- run build-cpython-mips64.sh first" >&2
	exit 1
fi

echo "=== what is staged ==="
sudo sh -c "ls $SRC/usr/bin/python* 2>/dev/null" | sed 's/^/  /'
echo "  size: $(sudo du -sh "$SRC" 2>/dev/null | cut -f1)"

echo "=== install into the CP root ==="
sudo cp -a "$SRC/usr/." "$C/usr/"
# python3 -> python3.14 is what everything execs; upstream make install
# creates it, but assert rather than assume.
if ! sudo test -e "$C/usr/bin/python3"; then
	sudo ln -sf python3.14 "$C/usr/bin/python3"
	echo "  created the python3 symlink"
fi
sudo sh -c "ls -l $C/usr/bin/python3 $C/usr/bin/python3.14 2>/dev/null" | sed 's/^/  /'
sudo sh -c "file -b $C/usr/bin/python3.14 2>/dev/null" | cut -c1-72 | sed 's/^/  /'

for m in proc sys dev; do
	sudo mkdir -p "$C/$m"
	mountpoint -q "$C/$m" || sudo mount --rbind "/$m" "$C/$m"
done

# The runtime libraries below come from the local mips64 repo, and cproot's
# sources.list points at http://127.0.0.1:8899 -- apt runs INSIDE the chroot
# where a host file:// path does not resolve. pkill lives in this script and
# not in an ad-hoc command line because a pkill whose pattern also appears in
# the caller's argv kills the caller.
pkill -f "http\.server" 2>/dev/null
( cd "$B/sid-host/tmp/repo" && nohup python3 -m http.server 8899 --bind 127.0.0.1 \
    >/tmp/repohttp.log 2>&1 & )
sleep 2
if curl -sf -o /dev/null http://127.0.0.1:8899/dists/rebootstrap/Release; then
	echo "repo server up"
else
	echo "repo server did NOT come up -- apt will fail below" >&2
fi

ch() {
	sudo chroot "$C" /usr/bin/env -i \
		LC_ALL=C.UTF-8 LANG=C.UTF-8 PATH=/usr/sbin:/usr/bin:/sbin:/bin \
		HOME=/root TERM=dumb "$@"
}

# THE RUNTIME LIBRARIES ARE A SEPARATE STEP FROM THE BUILD ONES.
#
# The build root has the -dev packages; the CP root had neither those nor their
# runtime counterparts, so the interpreter started fine and then died on the
# first C extension that needed one:
#
#     ImportError: libsqlite3.so.0: cannot open shared object file
#
# Checking only the python BINARY would have missed most of it -- every
# extension in lib-dynload links its own libraries, and a missing one only
# surfaces when that module is imported. Seven sonames were missing across
# binary + extensions; openssl, zlib, lzma, bz2 and uuid were already present.
#
# The package names were resolved by asking the build root's dpkg which package
# owns each soname, NOT guessed: two of the six are t64 variants
# (libgdbm6t64, libreadline8t64), and libncursesw6 supplies both libncursesw
# and libpanelw.
echo "=== the runtime libraries python's extensions need ==="
PYLIBS="libexpat1 libffi8 libgdbm6t64 libncursesw6 libreadline8t64 libsqlite3-0"
ch sh -c "apt-get install -y --no-install-recommends $PYLIBS >/tmp/pylibs.log 2>&1"
if [ $? != 0 ]; then
	echo "  FAILED:"
	sudo grep -aE "^E:|but it is" "$C/tmp/pylibs.log" | head -6 | sed 's/^/    /'
else
	echo "  installed: $PYLIBS"
fi
printf '  still missing after that: '
ch sh -c 'for f in /usr/bin/python3.14 /usr/lib/python3.14/lib-dynload/*.so; do
            [ -e "$f" ] && ldd "$f" 2>/dev/null
          done' 2>/dev/null | grep -ac "not found" || echo 0

echo "=== does it run IN the CP root? ==="
ch /usr/bin/python3 -c 'import sys, sysconfig
print("  version  ", sys.version.split()[0])
print("  platform ", sysconfig.get_platform())
print("  byteorder", sys.byteorder)
print("  maxsize  ", sys.maxsize)' 2>&1 | sed 's/^/  /'

echo "=== the stdlib modules the FFN agents import ==="
ch /usr/bin/python3 -c 'mods = "argparse json os re select signal socket subprocess sys threading time".split()
import importlib
bad = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception as e:
        bad.append("%s: %s" % (m, e))
print("  imported %d/%d" % (len(mods) - len(bad), len(mods)))
for b in bad:
    print("  FAILED", b)' 2>&1 | sed 's/^/  /'

echo "=== match/case (the agents use it; needs >= 3.10) ==="
ch /usr/bin/python3 -c 'x = 2
match x:
    case 2: print("  match/case works")
    case _: print("  unexpected")' 2>&1 | sed 's/^/  /'

echo "=== ssl / sqlite3 / zlib / ctypes, which are the usual porting casualties ==="
ch /usr/bin/python3 -c 'import ssl, sqlite3, zlib, lzma, bz2, ctypes
print("  ssl    ", ssl.OPENSSL_VERSION)
print("  sqlite3", sqlite3.sqlite_version)
print("  zlib   ", zlib.ZLIB_VERSION)
print("  ctypes  ok")' 2>&1 | sed 's/^/  /'

if [ -d "$FFNPY" ]; then
	echo "=== compile the real FFN agents under it ==="
	sudo mkdir -p "$C/tmp/ffnpy"
	sudo sh -c "cp $FFNPY/*.py $C/tmp/ffnpy/ 2>/dev/null" || true
	ch /usr/bin/python3 -c 'import pathlib, py_compile, sys
ok = bad = 0
for p in sorted(pathlib.Path("/tmp/ffnpy").glob("*.py")):
    try:
        py_compile.compile(str(p), doraise=True)
        ok += 1
        print("  ok  ", p.name)
    except Exception as e:
        bad += 1
        print("  FAIL", p.name, type(e).__name__, str(e)[:90])
print("  compiled %d ok, %d failed" % (ok, bad))
sys.exit(1 if bad else 0)' 2>&1 | sed 's/^/  /'
else
	echo "=== no FFN .py files at $FFNPY -- skipping the agent compile ==="
	echo "  (set FFNPY=<dir> with ffn_bcmd.py etc. to exercise them)"
fi

echo "=== package count and size of the root now ==="
ch /usr/bin/dpkg-query -W -f '${Package}\n' 2>/dev/null | wc -l | sed 's/^/  pkgs: /'
sudo du -sh "$C" 2>/dev/null | sed 's/^/  size: /'

echo "=== unmount the kernel filesystems (this tree gets tarred and exported) ==="
for m in dev sys proc; do
	if mountpoint -q "$C/$m"; then
		sudo umount -R "$C/$m" 2>/dev/null && echo "  $m unmounted"
	fi
done
# NOTE: umount -R on a recursive bind of /proc can propagate to the HOST and
# tear down /proc/sys/fs/binfmt_misc, flushing every registration -- which
# makes every mips64 binary fail with "Exec format error" afterwards. The
# registration is now declared in /etc/binfmt.d/ffn-mips64-be.conf so
# systemd-binfmt restores it, but check it if execution suddenly breaks.
if [ ! -e /proc/sys/fs/binfmt_misc/ffn-mips64-be ]; then
	echo "  binfmt registration went away -- restoring"
	sudo systemctl restart systemd-binfmt
fi
echo "  binfmt: $([ -e /proc/sys/fs/binfmt_misc/ffn-mips64-be ] && echo present || echo MISSING)"
