#!/usr/bin/env python3
r"""Apply FFN's edits to a rebootstrap bootstrap.sh, idempotently.

WHY THIS IS A FILE AND NOT A HEREDOC. It used to be Python embedded in
ffn-debian-mips64-bootstrap.sh via `python3 - <<'PY'`, and that was broken from
the moment it was committed: the string literals need backslash-t and
backslash-n as two-character escapes, and every pass through a shell heredoc
collapsed them into literal tabs and newlines. A single-quoted Python string
cannot span lines, so the embedded program was a SyntaxError. `sh -n` on the
wrapper does not look inside a heredoc, so it stayed green, and the live chroot
happened to be patched by separate ad-hoc scripts -- which is exactly why nobody
noticed.

That collapse happened SIX times in one session, including once while writing
this very file through a heredoc that was supposed to end the problem. The rule
that actually works: anything containing backslash escapes gets written as a
file directly, never through a heredoc.

Each edit checks a distinctive marker of its OWN rather than a substring that
might occur elsewhere. That mattered once already: a guard testing for
"debian/changelog" matched add_binNMU_changelog and reported "already wired"
while doing nothing at all.
"""

import io
import sys

EDITS = []


def edit(name, marker, old, new):
    """Register one idempotent substitution.

    name   - what to print
    marker - text present once the edit is applied; if found, skip
    old    - exact text to replace (must appear, or the edit is reported missing)
    new    - replacement
    """
    EDITS.append((name, marker, old, new))


# --- 1. fold non-ASCII maintainer names before dpkg ever parses them ---------
#
# dpkg 1.23.8 hands Dpkg::Email::Address undecoded bytes and its address grammar
# rejects them. There are TWO parse sites and both need covering:
#
#   debian/control    Maintainer: / Uploaders:      -> field_parse_uploaders
#   debian/changelog  " -- Name <email>  Date"      -> dpkg-parsechangelog
#
# The second surfaced on lmdb only after the first was fixed for cyrus-sasl2.
#
# `if` rather than `test ... && cmd`: a false test as the last statement of the
# loop would abort the whole bootstrap under set -e.
edit(
    "asciify Maintainer/Uploaders",
    "_asciify_f",
    '\tobtain_source_package "$pkg"\n'
    '\tcd "${pkg}-"*\n'
    '\thook=`get_hook patch "$pkg"` && "$hook"',

    '\tobtain_source_package "$pkg"\n'
    '\tcd "${pkg}-"*\n'
    '\tfor _asciify_f in debian/control debian/changelog; do\n'
    '\t\tif test -f "$_asciify_f"; then\n'
    '\t\t\tdrop_privs perl /usr/local/bin/asciify-control.pl "$_asciify_f"\n'
    '\t\tfi\n'
    '\tdone\n'
    '\thook=`get_hook patch "$pkg"` && "$hook"',
)


# --- 2. systemd: a dh_shlibdeps -l path must be ABSOLUTE and point at staging -
#
# debian/rules:299 passes a RELATIVE -l:
#
#     dh_shlibdeps -plibsystemd-shared -lusr/lib/$(DEB_HOST_MULTIARCH)/systemd
#
# dh_shlibdeps makes that absolute by prefixing a bare "/", so it searches
# /usr/lib/mips64-linux-gnuabi64/systemd on the BUILD HOST:
#
#     warning: directory /usr/lib/mips64-linux-gnuabi64/systemd for -l
#              does not exist
#     error: cannot find library libsystemd-shared-262.so needed by
#            debian/libsystemd-shared/.../libsystemd-core-262.so
#
# It works natively because systemd is already installed at that path; in a
# bootstrap only the staged copy exists. Verified on the real tree: the staged
# directory holds libsystemd-core-262.so and libsystemd-shared-262.so, exactly
# what the search was missing.
#
# THIRD TIME THIS SHAPE HAS APPEARED (glibc twice, now systemd), so state the
# rule once: a -l handed to dh_shlibdeps must be absolute AND point into
# debian/<pkg>/, never at where the library will eventually be installed.
#
# Inserted at the top of patch_systemd so it runs unconditionally -- that hook's
# existing body is guarded on non-glibc targets and never fires for
# mips64-linux-gnuabi64.
edit(
    "systemd shlibdeps -l absolute",
    "debian/libsystemd-shared/usr/lib",
    "patch_systemd() {\n",

    "patch_systemd() {\n"
    "\techo \"patching systemd: dh_shlibdeps -l must be an absolute staged"
    " path\"\n"
    "\tdrop_privs sed -i"
    " 's|-lusr/lib/$(DEB_HOST_MULTIARCH)/systemd"
    "|-l$(CURDIR)/debian/libsystemd-shared/usr/lib/$(DEB_HOST_MULTIARCH)"
    "/systemd|'"
    " debian/rules\n",
)


# --- 3. libcap-ng: drop the bluetooth build-dep via the package's own profile -
#
#     builddeps:./:mips64 : Depends: libbluetooth-dev:mips64
#                           but it is not installable
#
# libcap-ng-utils links bluetooth, and libcap-ng's own control already guards
# that dependency behind a build profile:
#
#     libbluetooth-dev <!pkg.libcap-ng.noutils>,
#
# rebootstrap was invoking it with "nopython" only, so the guard never engaged
# and apt was asked for a bluez stack that no part of this bootstrap builds.
# Nothing needs patching -- the package already offers the switch, it just was
# not thrown.
#
# This is the established idiom here rather than a novelty: the same file
# already does pkg.util-linux.noverity, pkg.db5.3.notcl,
# pkg.cyrus-sasl2.nogssapi/noldap/nosql and pkg.sysprof.nogui/nounwind.
#
# Cost is libcap-ng-utils (captest, filecap, netcap, pscap, execcap). None is a
# build-essential or debhelper dependency, which is the only thing this
# bootstrap has to reach.
edit(
    "libcap-ng noutils profile",
    "pkg.libcap-ng.noutils",
    "cross_build libcap-ng nopython libcap-ng_1",
    'cross_build libcap-ng "nopython pkg.libcap-ng.noutils" libcap-ng_1',
)


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: %s /path/to/bootstrap.sh" % sys.argv[0])
    path = sys.argv[1]
    text = io.open(path, encoding="utf-8", errors="surrogateescape").read()
    original = text
    rc = 0

    for name, marker, old, new in EDITS:
        if marker in text:
            print("  already applied: %s" % name)
            continue
        if old not in text:
            print("  ANCHOR MISSING: %s -- bootstrap.sh differs from what was"
                  " expected; not patched" % name)
            rc = 1
            continue
        text = text.replace(old, new, 1)
        print("  applied: %s" % name)

    if text != original:
        io.open(path, "w", encoding="utf-8", errors="surrogateescape",
                newline="").write(text)
    return rc


if __name__ == "__main__":
    sys.exit(main())
