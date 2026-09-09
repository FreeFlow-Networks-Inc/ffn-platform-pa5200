#!/bin/sh
# Build one Debian source package for mips64 BE in the native-under-qemu build
# root, and publish the result into the local repo so the next package can build
# against it.
#
#   build-deb-mips64.sh <source-package> [DEB_BUILD_PROFILES]
#
# e.g.  build-deb-mips64.sh zip
#       build-deb-mips64.sh libselinux "nopython noruby"
#
# WHY PROFILES ARE A FIRST-CLASS ARGUMENT. Twice a dependency that looked like it
# needed building turned out to be optional behind a build profile: libcap-ng's
# bluez dep behind pkg.libcap-ng.noutils, and libselinux's ENTIRE ruby+python3
# requirement behind <!nopython> <!noruby>. Checking for a profile is far cheaper
# than building a language runtime. Look first.
#
# The repo is served over HTTP because apt runs inside the chroot, where a host
# file:// path does not resolve.
set -u

SRC=${1:?usage: build-deb-mips64.sh <source-package> [profiles]}

# nodoc and nocheck are ALWAYS on, plus whatever the caller adds.
#
# nodoc is the single highest-value profile here. Documentation build-deps are
# where the heavy, irrelevant toolchains live, and they are usually annotated
# <!nodoc>, e.g. libmnl:
#
#   Build-Depends:       debhelper-compat (= 13), libtool, pkgconf
#   Build-Depends-Indep: doxygen <!nodoc>, graphviz <!nodoc>
#
# With nodoc (or --arch-only, below) libmnl needs NOTHING that is not already
# built. Without it, it wants doxygen and graphviz and looks like a dead end.
PROFILES="nodoc nocheck${2:+ $2}"

B=/mnt/clones/debian-mips64
R=$B/sid-host/tmp/repo
T=$B/buildroot
PORT=8899
LOG=$B/logs/build-$SRC.log

# nocheck by default. A testsuite runs the JUST-BUILT target binaries under
# qemu, where timing, /proc details and locale behaviour differ from real
# hardware, so a failure there says little about the package. rebootstrap made
# the same call for the same reason (its line 7 sets
# DEB_BUILD_OPTIONS="nocheck noddebs parallel=1").
#
# A real trade, not a freebie: busybox failed exactly two of its own tests here
# -- "printf understands %s" and "printf handles positive numbers for %f" -- and
# skipping the suite means nobody looks at them. %f on a fresh big-endian port
# deserves a look on REAL hardware, where the result would mean something.
# NOCHECK=0 runs testsuites anyway.
OPTS="noddebs parallel=1"
[ "${NOCHECK:-1}" = 1 ] && OPTS="nocheck $OPTS"

pkill -f "http.server $PORT" 2>/dev/null
( cd "$R" && nohup python3 -m http.server "$PORT" --bind 127.0.0.1 \
    >/tmp/repohttp.log 2>&1 & )
sleep 2

for m in proc sys dev; do
	sudo mkdir -p "$T/$m"
	mountpoint -q "$T/$m" || sudo mount --rbind "/$m" "$T/$m"
done
sudo cp /etc/resolv.conf "$T/etc/resolv.conf" 2>/dev/null

# A FUNCTION, not a string. Both DEB_BUILD_OPTIONS and DEB_BUILD_PROFILES hold
# SPACE-separated lists, so building one "env -i VAR=..." string and expanding
# it unquoted word-splits the values apart.
#
# And the two variables do NOT take the same separator. DEB_BUILD_PROFILES is
# space-separated for dpkg but comma-separated when passed to apt as -P.
# Comma-joining DEB_BUILD_OPTIONS -- which I did first -- makes dpkg reject the
# whole thing as one bad flag:
#   dpkg-buildpackage: warning: invalid flag in DEB_BUILD_OPTIONS:
#       nocheck,noddebs,parallel=1
# and the testsuite then runs regardless, which is exactly how this was found.
ch() {
	sudo chroot "$T" /usr/bin/env -i \
		LC_ALL=C.UTF-8 LANG=C.UTF-8 \
		PATH=/usr/sbin:/usr/bin:/sbin:/bin \
		HOME=/root TERM=dumb DEBIAN_FRONTEND=noninteractive \
		DEB_BUILD_OPTIONS="$OPTS" \
		DEB_BUILD_PROFILES="$PROFILES" \
		"$@"
}

# apt wants the profile list comma-separated; dpkg wants it space-separated.
PROFARG=""
[ -n "$PROFILES" ] && PROFARG="-P$(echo "$PROFILES" | tr ' ' ',')"

echo "=== $SRC ${PROFILES:+(profiles: $PROFILES)} ==="
echo "  DEB_BUILD_OPTIONS=$OPTS"
ch apt-get update -qq 2>&1 | tail -1

echo "--- build-deps ---"
# --arch-only: install Build-Depends but NOT Build-Depends-Indep.
#
# READ THE WHOLE SOURCE RECORD, NOT JUST Build-Depends. Parsing only the
# Build-Depends field made libmnl look dependency-free when its doxygen and
# graphviz requirements were sitting in Build-Depends-Indep the whole time --
# a claim I made and had to retract. "apt-cache showsrc <pkg> | grep ^Build-"
# shows every field; sed-ing out only Build-Depends hides half the problem.
#
# Paired with dpkg-buildpackage -B below, so the arch-independent binaries are
# never attempted and their build-deps are genuinely not needed.
ch apt-get build-dep -y --arch-only --no-install-recommends $PROFARG "$SRC" \
	>"$T/tmp/bd.log" 2>&1
if [ $? != 0 ]; then
	echo "  FAILED to install build-deps"
	sudo grep -aE "Depends:|but it is|^E:" "$T/tmp/bd.log" | head -8 | sed 's/^/    /'
	exit 1
fi
echo "  installed"

WORK=/build/$SRC
sudo rm -rf "$T$WORK"
sudo mkdir -p "$T$WORK"
echo "--- source ---"
ch sh -c "cd $WORK && apt-get source $SRC >/dev/null 2>&1" || {
	echo "  FAILED to fetch source"; exit 1; }
D=$(sudo sh -c "ls -d $T$WORK/*/ 2>/dev/null | head -1")
[ -z "$D" ] && { echo "  no source tree"; exit 1; }
echo "  ${D#$T}"

echo "--- dpkg-buildpackage (arch-specific binaries only) ---"
# -B not -b : arch-specific binaries ONLY. The arch:all packages of these
# sources are documentation, and building them would drag in the very
# Build-Depends-Indep toolchain --arch-only just skipped. Every library the
# chain actually needs (libmnl-dev, libbpf-dev, libxtables-dev ...) is
# arch-specific.
#
# -r'' : we are root in the chroot and fakeroot is not built for mips64.
# --no-check-builddeps: apt-get build-dep already did it, and under profiles
# dpkg's own check can disagree with apt's resolution.
ch sh -c "cd ${D#$T} && dpkg-buildpackage -B -uc -us -r'' --no-check-builddeps" \
	>"$LOG" 2>&1
rc=$?
echo "  exit=$rc"
if [ "$rc" != 0 ]; then
	echo "  --- errors ---"
	# -a because build logs carry control characters; plain grep then reports
	# only "binary file matches" and hides every error. Same trap as the bcmd
	# log.
	sudo grep -anE "internal compiler error|error:|Error [0-9]|cannot find|undefined reference|dh_.*: error|\*\*\*" \
		"$LOG" | tail -12 | sed 's/^/    /'
	echo "  full log: $LOG"
	exit 1
fi

# Publish with the reprepro INSIDE sid-host, not the host's. The host has none,
# and that chroot's reprepro created this repo's db -- a different version risks
# disagreeing with it. The .debs are staged into sid-host/tmp/incoming so they
# are reachable from in there.
echo "--- publish into the local repo ---"
INC=$B/sid-host/tmp/incoming
sudo mkdir -p "$INC"
sudo sh -c "cp $T$WORK/*.deb $INC/ 2>/dev/null" || true
n=0
for deb in $(sudo sh -c "ls $INC/*.deb 2>/dev/null"); do
	base=$(basename "$deb")
	if sudo chroot "$B/sid-host" reprepro -b /tmp/repo includedeb rebootstrap \
		"/tmp/incoming/$base" >/dev/null 2>&1; then
		n=$((n+1))
	else
		echo "    reprepro rejected $base"
	fi
done
sudo sh -c "rm -f $INC/*.deb" 2>/dev/null
echo "  published $n .deb(s)"
sudo sh -c "ls $T$WORK/*.deb 2>/dev/null" | xargs -n1 basename 2>/dev/null | sed 's/^/    /'
