#!/bin/sh
# Assemble a Debian mips64 big-endian root from the locally bootstrapped
# packages plus the Debian archive for Architecture: all.
#
# Four things here are not obvious and each one cost a run:
#
# 1. TWO SOURCES, EACH PINNED BY ARCHITECTURE. deb.debian.org has no
#    main/binary-mips64 -- that is the entire reason this port exists -- so it
#    is restricted to arch=all and the mips64 packages come from the local
#    repo. Without the pins apt tries to fetch binary-mips64 from Debian and
#    dies exactly the way debootstrap does.
#
# 2. THE LOCAL REPO MUST ANSWER TO THE SUITE NAME. mmdebstrap picks the
#    essential set with an apt pattern:
#
#      ?narrow(?or(?archive(^sid$),?codename(^sid$)),?architecture(mips64),?essential)
#
#    The repo's Release said only "Codename: rebootstrap", so every locally
#    built package was narrowed OUT and the run installed just the 7 arch:all
#    packages from Debian -- 313 kB, and a root with no dpkg in it. A
#    "Suite: sid" line in that Release fixes it. Verified separately that apt
#    itself was always fine: it saw 37636 packages and all 21 Essential ones.
#
# 3. SERVE THE REPO OVER HTTP, NOT file://. mmdebstrap runs the apt phase
#    INSIDE the target chroot, where an absolute host path does not resolve:
#
#      E: package file .../pool/main/g/glibc/libc6_2.43-5_mips64.deb
#         not accessible from ...
#
# 4. mips64 NEEDS /lib64 AND mmdebstrap DOES NOT CREATE IT. It makes the
#    merged-/usr symlinks for bin, sbin and lib but not lib64, because most
#    architectures have no /lib64. mips64 n64 binaries name /lib64/ld.so.1 as
#    their interpreter, and the loader lands at /usr/lib64/ld.so.1, so without
#    the symlink the very first target binary fails:
#
#      qemu-mips64: Could not open '/lib64/ld.so.1': No such file or directory
#
#    The extract hook runs after unpacking and before dpkg is executed, which
#    is exactly the window where the symlink has to appear.
#
# Execution of the target's own mips64 binaries relies on the ffn-mips64-be
# binfmt registration (flags F, interpreter .../sid-host/usr/bin/qemu-mips64).
# mmdebstrap cannot see it -- it probes via update-binfmts, which is not
# installed -- hence --skip=check/qemu. Proven working beforehand: a static
# mips64 binary printed sizeof(long)=8 and BIG endian under that interpreter.
set -u

B=/mnt/clones/debian-mips64
R=$B/sid-host/tmp/repo
T=${1:-$B/cproot}
LOG=$B/logs/mmdebstrap-cproot.log
PORT=8899
VARIANT=${VARIANT:-required}

pkill -f "http.server $PORT" 2>/dev/null
( cd "$R" && nohup python3 -m http.server "$PORT" --bind 127.0.0.1 \
    >/tmp/repohttp.log 2>&1 & )
sleep 2
code=$(curl -s -o /dev/null -w '%{http_code}' \
       "http://127.0.0.1:$PORT/dists/rebootstrap/Release")
if [ "$code" != "200" ]; then
	echo "repo not being served (http=$code)" >&2
	exit 1
fi
echo "local repo served on 127.0.0.1:$PORT (Release -> $code)"

LOCAL="deb [trusted=yes arch=mips64] http://127.0.0.1:$PORT/ rebootstrap main"
DEBIAN="deb [arch=all signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://deb.debian.org/debian sid main"

sudo rm -rf "$T"
sudo mkdir -p "$T"

echo "building variant=$VARIANT into $T"
sudo mmdebstrap \
	--arch=mips64 \
	--variant="$VARIANT" \
	--skip=check/qemu \
	--verbose \
	--extract-hook='ln -sfn usr/lib64 "$1/lib64"' \
	sid "$T" "$LOCAL" "$DEBIAN" > "$LOG" 2>&1
rc=$?

echo "mmdebstrap exit=$rc"
tail -8 "$LOG" | cut -c1-125
exit "$rc"
