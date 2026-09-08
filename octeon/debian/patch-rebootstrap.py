#!/usr/bin/env python3
"""Apply FFN's edits to a rebootstrap bootstrap.sh, idempotently.

WHY THIS IS A FILE AND NOT A HEREDOC. It used to be Python embedded in
ffn-debian-mips64-bootstrap.sh via `python3 - <<'PY'`, and that was broken from
the moment it was committed: the string literals need "\\t" and "\\n" as
two-character escapes, and every pass through a shell heredoc collapsed them to
literal tabs and newlines. A single-quoted Python string cannot span lines, so
the embedded program was a SyntaxError. `sh -n` on the wrapper does not look
inside a heredoc, so it stayed green, and the live chroot happened to be patched
by separate ad-hoc scripts -- which is exactly why nobody noticed.

That collapse happened five times in one session across different files. A
standalone script cannot hit it at all: nothing rewrites its contents on the way
to disk. Anything of this shape belongs in its own file.

Each edit checks for a distinctive marker of its own rather than for a substring
that might occur elsewhere. That mattered once already: a guard testing for
"debian/changelog" matched add_binNMU_changelog and reported "already wired"
while doing nothing.
"""

import io
import sys

EDITS = []


def edit(name, marker, old, new):
    EDITS.append((name, marker, old, new))


# --- 1. glibc: dh_shlibdeps has no -l path, and a stamp path has a double slash
edit(
    "glibc stamp path",
    "stamp)build_libc",
    "\t\tdrop_privs sed -i 's,$(stamp)/build_libc,$(stamp)build_libc,g'"
    " debian/rules.d/build.mk\n",
    None,   # informational: applied by the wrapper's own patch_glibc block
)

# --- 2. fold non-ASCII maintainer names before dpkg ever parses them
#
# dpkg 1.23.8 hands Dpkg::Email::Address undecoded bytes and its grammar
# rejects them. There are TWO parse sites and both had to be covered:
#
#   debian/control    Maintainer: / Uploaders:
#   debian/changelog  the " -- Name <email>  Date" trailer
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


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: %s /path/to/bootstrap.sh" % sys.argv[0])
    path = sys.argv[1]
    text = io.open(path, encoding="utf-8", errors="surrogateescape").read()
    original = text
    rc = 0

    for name, marker, old, new in EDITS:
        if new is None:
            continue
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
