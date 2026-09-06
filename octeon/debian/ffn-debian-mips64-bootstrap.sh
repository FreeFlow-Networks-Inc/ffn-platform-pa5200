#!/bin/sh
# Bootstrap Debian for mips64 (BIG-endian) from source. Runs on the build VM.
#
# Debian has no mips64 archive -- see ../USERLAND-DISTRO.md for the measurements.
# So this is not an install, it is a PORT: cross-build a toolchain, then build
# the base system from Debian source packages, then keep building.
#
# WHAT IS ALREADY IN OUR FAVOUR, and it is more than it looks:
#
#   * dpkg ALREADY KNOWS mips64. `dpkg-architecture -a mips64 -q
#     DEB_HOST_GNU_TYPE` answers mips64-linux-gnuabi64, and the multiarch tuple
#     is the same. The architecture is defined in Debian's tables even though no
#     buildd produces it, so nothing here has to patch dpkg or invent a triplet.
#   * Debian's own `rebootstrap` exists for exactly this: cross-bootstrapping an
#     architecture that has no archive. It sequences the awkward part -- gcc
#     stage1 -> linux headers -> glibc stage1 -> gcc stage2 -> glibc -> gcc
#     stage3 -- which is the bit that defeats a hand-rolled attempt.
#
# WHY A DEBIAN SID CHROOT AND NOT THE HOST DIRECTLY. rebootstrap builds Debian
# source packages and calls `apt-get build-dep`; it expects Debian unstable. The
# build VM is Ubuntu, whose versions and package names drift from sid in ways
# that surface as unrelated build failures deep in a multi-hour run. A sid
# chroot costs one download and removes that entire class of problem.
#
# ENDIANNESS IS THE WHOLE POINT. mips64 in Debian's tables is BIG-endian;
# mips64el is the little-endian one and is the architecture that actually has an
# archive. It would be very easy to bootstrap the wrong one and not notice until
# the first binary refuses to run on the target. Every stage below asserts
# big-endian, and the final check greps for "MSB".
set -eu

BASE=${BASE:-/mnt/clones/debian-mips64}
HOST_ARCH=${HOST_ARCH:-mips64}
CHROOT=$BASE/sid-host
LOGS=$BASE/logs
MIRROR=${MIRROR:-https://deb.debian.org/debian}
JOBS=${JOBS:-$(nproc)}

say() { echo "  debian-mips64: $*"; }
inch() { sudo chroot "$CHROOT" /bin/sh -c "$1"; }

# --- 0. sanity: are we building the endianness we think we are? ------------
# Ask dpkg rather than trusting the name. mips64 vs mips64el is one character
# and the failure mode is a whole distribution that will not run.
EXPECT_TRIPLET=mips64-linux-gnuabi64
GOT=$(dpkg-architecture -a "$HOST_ARCH" -q DEB_HOST_GNU_TYPE 2>/dev/null || true)
[ "$GOT" = "$EXPECT_TRIPLET" ] || {
	say "HOST_ARCH=$HOST_ARCH resolves to '$GOT', expected $EXPECT_TRIPLET"
	say "mips64 is BIG-endian; mips64el is little-endian. Check before building."
	exit 2
}
say "target $HOST_ARCH -> $GOT (big-endian, n64)"

# --- 1. the sid build chroot ----------------------------------------------
if [ -x "$CHROOT/usr/bin/dpkg" ]; then
	say "sid chroot present"
else
	say "building sid chroot (this downloads a few hundred MB)"
	mkdir -p "$LOGS"
	sudo debootstrap --variant=buildd \
		--include=git,make,curl,ca-certificates,xz-utils,wget,gnupg \
		sid "$CHROOT" "$MIRROR" > "$LOGS/debootstrap.log" 2>&1 \
		|| { say "debootstrap failed -- see $LOGS/debootstrap.log"; exit 3; }
	say "sid chroot built"
fi

# rebootstrap needs source packages, so deb-src must be enabled. buildd variant
# does not include it.
inch 'grep -q deb-src /etc/apt/sources.list 2>/dev/null || printf "deb '"$MIRROR"' sid main\ndeb-src '"$MIRROR"' sid main\n" > /etc/apt/sources.list'
inch 'mkdir -p /proc /sys /dev/pts'
sudo mount --bind /proc "$CHROOT/proc" 2>/dev/null || true
sudo mount --bind /sys  "$CHROOT/sys"  2>/dev/null || true
sudo mount --bind /dev/pts "$CHROOT/dev/pts" 2>/dev/null || true

inch 'apt-get update -qq' >/dev/null 2>&1 || { say "apt-get update failed in chroot"; exit 3; }
say "chroot apt ready: $(inch 'apt-cache policy 2>/dev/null | head -1' || echo '?')"

# --- 2. rebootstrap -------------------------------------------------------
if [ -d "$CHROOT/root/rebootstrap/.git" ]; then
	say "rebootstrap present"
else
	say "cloning rebootstrap"
	inch 'cd /root && git clone --depth 1 https://salsa.debian.org/helmutg/rebootstrap.git' \
		> "$LOGS/clone.log" 2>&1 || { say "clone failed -- see $LOGS/clone.log"; exit 4; }
fi

# --- 3. run it ------------------------------------------------------------
# SETTINGS GO AS ARGUMENTS, NOT AS ENVIRONMENT VARIABLES. bootstrap.sh does
#
#     HOST_ARCH=undefined            # its own default, unconditional
#     ...
#     for param in "$@"; do eval $param; done
#
# so it assigns its defaults FIRST and then evals the command line. An exported
# HOST_ARCH is therefore silently overwritten, and the run dies four seconds in
# with
#
#     dpkg-architecture: error: unknown Debian architecture undefined
#     architecture undefined unknown to dpkg
#
# which names dpkg and the word "undefined" and not the actual mistake. Got this
# wrong on the first attempt.
#
# ENABLE_MULTIARCH_GCC=no: the multiarch-gcc path needs cross-toolchain packages
# that only exist for architectures with an archive, which is precisely what
# mips64 does not have. The classic path builds gcc from source at each stage
# instead -- slower, and the one that works for a new architecture.
#
# MAKEFLAGS stays an environment variable: that one is read by make, not by
# bootstrap.sh, so it is not subject to the above.
#
# Not backgrounded here: this runs for hours and its log is the deliverable, so
# the caller decides how to run it (nohup, tmux, a systemd unit). Printing the
# command rather than hiding it also means a failed stage can be re-run by hand.
CMD="cd /root/rebootstrap && MAKEFLAGS=-j$JOBS ./bootstrap.sh HOST_ARCH=$HOST_ARCH ENABLE_MULTIARCH_GCC=no"

say "ready. The bootstrap itself is long-running; start it with:"
echo
echo "    sudo chroot $CHROOT /bin/sh -c '$CMD' 2>&1 | tee $LOGS/bootstrap.log"
echo
cat <<EOF
Stages, in the order they must succeed -- each one's failure looks different:
  1. binutils            cross-assembler/linker for mips64-linux-gnuabi64
  2. gcc stage1          C only, no libc yet
  3. linux headers       the kernel's exported ABI
  4. glibc stage1        headers + startup files
  5. gcc stage2          enough to build a real glibc
  6. glibc               the real one
  7. gcc stage3          full compiler against that glibc
  8. base system         ~1000 source packages

VERIFY THE ENDIANNESS AS SOON AS STAGE 7 PRODUCES A BINARY -- do not wait for
stage 8. A whole run can complete for the wrong architecture:

    file <any built binary>     # must say: ELF 64-bit MSB ... MIPS64
                                # MSB = big-endian. LSB means mips64el and
                                # the result cannot run on our hardware.
EOF
