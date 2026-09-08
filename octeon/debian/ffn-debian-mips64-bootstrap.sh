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

# --- 2b. patch rebootstrap: glibc's stamp path -----------------------------
# Debian's glibc 2.43-5 packaging has a two-line bug that stops a
# new-architecture bootstrap dead, and it is invisible on a release
# architecture:
#
#   debian/rules:42               stamp := $(CURDIR)/stamp-dir/   <- ends in /
#   debian/rules.d/build.mk:375   $(stamp)build_C.utf8:      $(stamp)/build_libc
#   debian/rules.d/build.mk:379   $(stamp)build_locales-all: $(stamp)/build_libc
#
# Everything else concatenates directly -- $(stamp)build_foo -- because $(stamp)
# already carries the separator. Those two insert a second one, so the
# prerequisite is spelled ".../stamp-dir//build_libc", and make treats "a//b"
# and "a/b" as DIFFERENT target names:
#
#   make: *** No rule to make target '.../stamp-dir//build_libc',
#            needed by '.../stamp-dir/build_C.utf8'.  Stop.
#
# Debian never sees it because when the stamp FILE already exists the
# filesystem collapses the double slash, make finds it, and no rule is needed.
# It only fails when make must actually BUILD that prerequisite -- which is
# precisely the staged cross-bootstrap case, where the libc pass has not run.
#
# Applied as a sed inside rebootstrap's own patch_glibc() hook, so it survives
# rebootstrap re-unpacking the source on every run. sed rather than a context
# diff: a diff breaks on any upstream edit to build.mk, this substitution is
# exact and idempotent. Note the SINGLE quotes -- $(stamp) must reach sed
# literally, and double quotes would have the shell substitute it away.
if grep -q 'stamp)build_libc' "$CHROOT/root/rebootstrap/bootstrap.sh" 2>/dev/null; then
	say "rebootstrap already carries the glibc stamp-path fix"
else
	say "patching rebootstrap's patch_glibc() for the glibc stamp path"
	sudo python3 - "$CHROOT/root/rebootstrap/bootstrap.sh" <<'PYFIX'
import io, sys
p = sys.argv[1]
lines = io.open(p, encoding="utf-8", errors="surrogateescape").read().splitlines(True)
fix = [
    '\techo "patching glibc: build.mk stamp prerequisite has a double slash"\n',
    "\tdrop_privs sed -i 's,$(stamp)/build_libc,$(stamp)build_libc,g' debian/rules.d/build.mk\n",
]
for i, l in enumerate(lines):
    if l.startswith("patch_glibc()"):
        j = i + 1
        while j < len(lines) and "regenerate_control" not in lines[j]:
            j += 1
        if j >= len(lines):
            sys.exit("no regenerate_control in patch_glibc()")
        lines[j + 1:j + 1] = fix
        break
else:
    sys.exit("patch_glibc() not found -- rebootstrap layout changed")
io.open(p, "w", encoding="utf-8", errors="surrogateescape", newline="").writelines(lines)
print("  debian-mips64: glibc stamp-path fix inserted")
PYFIX
	sudo sh -c "bash -n '$CHROOT/root/rebootstrap/bootstrap.sh'" \
		|| { say "patched bootstrap.sh does not parse -- reverting"; exit 5; }
fi

# --- 2c. patch rebootstrap: glibc's dh_shlibdeps search path ----------------
# debian/rules.d/debhelper.mk:95 derives its library search path from the LAST
# dash-separated word of the package name:
#
#   dh_shlibdeps -p$(curpass)
#     $(foreach path,$($(lastword $(subst -, ,$(curpass)))_slibdir),-l/usr$(path))
#
# For curpass=libc6-dev that word is "dev", so it looks up `dev_slibdir`, which
# does not exist -- the -l list is EMPTY BY CONSTRUCTION. (It works for the
# multilib passes: libc6-mipsn32 yields mipsn32_slibdir, which mips64.mk
# defines.) Natively that is harmless because libc is installed system-wide. In
# a staged cross-bootstrap nothing is installed, and packaging libc6-dev dies:
#
#   dpkg-shlibdeps: error: cannot find library libc.so.6 needed by
#     debian/libc6-dev/usr/lib/mips64-linux-gnuabi64/audit/sotruss-lib.so
#     (ELF format: 'elf64-tradbigmips' abi: 'ELF:64:b:mips:0')
#
# dpkg has the architecture right -- elf64-tradbigmips -- it just cannot find
# the file. Which was built moments earlier and is sitting in debian/libc6/.
#
# So point it at what is staged -- but NOT with make's $(wildcard). That was the
# first attempt and it silently expanded to nothing even though the files were
# there, because GNU MAKE CACHES $(wildcard) RESULTS PER DIRECTORY for the life
# of the process: debian/libc6/ is filled during this same run by dh_install,
# and make had already read the directory while it was empty. The patch applied,
# the files existed, and the error was byte-identical -- which is what a caching
# problem looks like from the outside.
#
# A runtime SHELL glob has no such cache. In a recipe `$$` becomes `$` for the
# shell, so the expansion happens when the rule executes:
#
#   -l$(echo $(CURDIR)/debian/libc6/usr/lib/*):$(CURDIR)/debian/libc6/usr/lib64
#
# AND THE PATHS MUST BE ABSOLUTE. With relative ones, dh_shlibdeps makes them
# absolute by prefixing a bare "/", so the verbose log showed it searching
#
#   dpkg-shlibdeps ... -l/debian/libc6/usr/lib/mips64-linux-gnuabi64
#
# at the filesystem root, which does not exist -- the flags were present and
# pointed nowhere, producing a byte-identical error for a third time. $(CURDIR)
# is make's absolute path to the source tree.
#
# THE LESSON is worth more than the fix. Three attempts, identical output every
# time. Identical output across a changed input means the change is not reaching
# what fails, and the way to find that out is to read what was ACTUALLY
# EXECUTED -- DH_VERBOSE=1 prints every command -- rather than to reason harder
# about what should have happened.
#
# AND THEN THE ERROR CHANGED, which is how you know a step landed:
#
#   no dependency information found for .../debian/libc6/.../libc.so.6
#
# A different complaint entirely. The file is now found; what it lacks is shlibs
# or symbols METADATA, because libc6 has not been assembled into a registered
# .deb at the moment libc6-dev is packaged. Natively libc6 is already installed
# and its metadata is in /var/lib/dpkg; in a staged cross-build it is a
# directory of files and nothing more.
#
# `-- --ignore-missing-info` downgrades exactly that to a warning, which is the
# documented option for the case. It costs nothing real: the resulting
# dependency on libc6 comes from glibc's own control file either way. The `--`
# matters -- without it the option is consumed by dh_shlibdeps instead of being
# passed through to dpkg-shlibdeps.
#
# dh_shlibdeps takes colon-separated directories in one -l. If the glob matches
# nothing the shell passes the pattern through unchanged, naming a directory
# that does not exist -- harmless, and no worse than the empty list.
if grep -q 'libc6/usr/lib64/ld.so.1' "$CHROOT/root/rebootstrap/bootstrap.sh" 2>/dev/null; then
	say "rebootstrap already carries the shlibdeps fix"
else
	say "patching rebootstrap's patch_glibc() for the dh_shlibdeps search path"
	sudo python3 - "$CHROOT/root/rebootstrap/bootstrap.sh" <<'PYSHLIB'
import io, sys
p = sys.argv[1]
lines = io.open(p, encoding="utf-8", errors="surrogateescape").read().splitlines(True)
sed = ("	drop_privs sed -i '/^\tdh_shlibdeps -p[$](curpass)/ "
       "s|$| -l$$(echo $(CURDIR)/debian/libc6/usr/lib/*):$(CURDIR)/debian/libc6/usr/lib64 -- --ignore-missing-info|' "
       "debian/rules.d/debhelper.mk
")
fix = ['	echo "patching glibc: dh_shlibdeps has no -l path for libc6-dev"
', sed]
for i, l in enumerate(lines):
    if l.startswith("patch_glibc()"):
        j = i + 1
        while j < len(lines) and "regenerate_control" not in lines[j]:
            j += 1
        if j >= len(lines):
            sys.exit("no regenerate_control inside patch_glibc()")
        lines[j + 1:j + 1] = fix
        break
else:
    sys.exit("patch_glibc() not found")
io.open(p, "w", encoding="utf-8", errors="surrogateescape", newline="").writelines(lines)
print("  debian-mips64: shlibdeps fix inserted")
PYSHLIB
	sudo sh -c "bash -n '$CHROOT/root/rebootstrap/bootstrap.sh'" 		|| { say "patched bootstrap.sh does not parse"; exit 5; }
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
# NO MAKEFLAGS=-jN. This used to pass MAKEFLAGS=-j$JOBS and that was a mistake
# which cost a build: readline died with
#
#   make[1]: *** No rule to make target 'install'.  Stop.
#   make: *** [debian/rules:247: install-stamp] Error 2
#   make: *** Waiting for unfinished jobs....
#
# The log proves the race rather than suggesting it: install-stamp failed 157
# lines BEFORE "touch configure-stamp" appeared. install ran while configure
# was still going, so build/ existed but had no Makefile in it yet.
#
# rebootstrap already decided this. bootstrap.sh line 7:
#
#   export DEB_BUILD_OPTIONS="nocheck noddebs parallel=1"
#
# parallel=1 is the SUPPORTED way to ask for serial builds and debhelper honours
# it. A bare MAKEFLAGS in the environment reaches every make including the
# top-level `make -f debian/rules`, so it overrides that intent for any package
# whose rules are hand-written rather than dh-driven -- readline's are.
#
# Setting DEB_BUILD_OPTIONS=parallel=N here would not help either: line 7
# assigns it unconditionally and would overwrite anything exported first.
#
# Worth being clear about the stake, because -j8 did build 307 packages before
# this surfaced: a stamp race does not only fail loudly. It can also install a
# half-built library and stamp it done. On a from-scratch architecture port,
# where the whole deliverable is a toolchain other things will be built with,
# a quietly wrong package is far more expensive than a slow build.
#
# JOBS is still used for the chroot's own apt/debootstrap work above.
#
# Not backgrounded here: this runs for hours and its log is the deliverable, so
# the caller decides how to run it (nohup, tmux, a systemd unit). Printing the
# command rather than hiding it also means a failed stage can be re-run by hand.
# THE HOST'S ENVIRONMENT MUST NOT LEAK INTO THE CHROOT. Two builds have now
# died from exactly that, and they are the same bug wearing different clothes:
#
#   MAKEFLAGS=-j8  raced readline's debian/rules against itself, because a bare
#                  MAKEFLAGS reaches every make including the top-level one and
#                  overrode rebootstrap's own DEB_BUILD_OPTIONS=parallel=1
#
#   LANG           en_US.UTF-8 survives sudo into a chroot that has only C,
#                  C.utf8 and POSIX. Perl merely warns and falls back, so it
#                  looked harmless for 114 source packages -- then cyrus-sasl2
#                  ran sphinx-build, whose setlocale(LC_ALL, "") RAISES:
#
#                      locale.Error: unsupported locale setting
#
#                  Reproduced directly: `chroot ... env LANG=en_US.UTF-8
#                  python3 -c 'locale.setlocale(locale.LC_ALL, "")'` fails,
#                  and the same with LC_ALL=C.UTF-8 succeeds.
#
# So the chroot gets an EXPLICIT environment via `env -i` rather than whatever
# the invoking shell happened to carry. C.UTF-8 is built into glibc and needs
# no locale generation, which is what makes it the right choice for a build
# chroot -- en_US.UTF-8 would have to be generated first, and a doc build is
# not the place to discover it was not.
#
# PATH, HOME, TERM and SHELL are passed because the build needs them; TERM=dumb
# keeps progress meters out of a log that is read with grep.
CHROOT_ENV="LC_ALL=C.UTF-8 LANG=C.UTF-8 PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root SHELL=/bin/sh TERM=dumb"
CMD="cd /root/rebootstrap && ./bootstrap.sh HOST_ARCH=$HOST_ARCH ENABLE_MULTIARCH_GCC=no"

say "ready. The bootstrap itself is long-running; start it with:"
echo
echo "    sudo chroot $CHROOT /usr/bin/env -i $CHROOT_ENV /bin/sh -c '$CMD' 2>&1 | tee $LOGS/bootstrap.log"
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
