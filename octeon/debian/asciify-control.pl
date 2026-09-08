#!/usr/bin/perl
# ASCII-fold ONLY the Maintainer and Uploaders fields of a debian/control file.
#
# WHY THIS IS NEEDED. dpkg 1.23.8 cannot parse a non-ASCII maintainer name:
#
#     dpkg-source: error: cannot parse Uploaders field value
#                         "Ondřej Surý <ondrej@debian.org>"
#
# Established by bisection, not guessed:
#
#     ASCII name                  PARSE OK
#     UTF-8 name                  FAIL
#     UTF-8 name + trailing comma FAIL
#     ASCII name + trailing comma PARSE OK   <- comma is tolerated
#
# and the mechanism, by feeding the parser both forms of the same string:
#
#     raw UTF-8 bytes             FAIL
#     decoded characters          PARSE OK
#
# So dpkg hands Dpkg::Email::AddressList undecoded bytes and its address
# grammar rejects them. It is architecture-independent -- it fails the same way
# on amd64 -- and it is not caused by the build environment: the failure is
# byte-identical with the host's inherited LANG and with an explicit
# LC_ALL=C.UTF-8.
#
# WHY NOT PATCH dpkg. It is the tool every package in the bootstrap is built
# with; a local divergence there is a worse thing to own than a cosmetic change
# to two metadata fields. And why not patch just cyrus-sasl2: it is the first
# such package in 114, not the last -- Debian has plenty of maintainers whose
# names are not ASCII, and ~900 packages remain.
#
# TWO PARSE SITES, TWO FILE TYPES. dpkg reads a maintainer name in two places
# and fails the same way in both:
#
#   debian/control     Maintainer: / Uploaders:      -> field_parse_uploaders
#   debian/changelog   the " -- Name <email>  Date"  -> dpkg-parsechangelog
#                      trailer
#
# The second one surfaced on lmdb after the first was fixed:
#
#     dpkg-buildpackage: error: cannot parse maintainer email address
#         "Ondrej Sury <ondrej@debian.org>" from changelog entry
#
# so this handles both, keyed on the file's basename.
#
# WHY NOT PATCH dpkg, now that it is clearly systemic. Dpkg::Email::Address is
# a single choke point and decoding its input there would fix every caller at
# once -- it is arguably the real bug, since an address grammar should run on
# characters and not bytes. It is rejected anyway because apt WILL overwrite it:
# there is direct evidence dpkg was upgraded inside this chroot mid-build (the
# first cyrus-sasl2 attempt parsed the same field that the second rejected). A
# fix that a build-dep install silently reverts is worse than none. This fold
# lives in cross_build_setup, which apt cannot touch.
#
# SCOPE IS DELIBERATELY NARROW. In control, only Maintainer and Uploaders with
# their continuation lines; in changelog, only the trailer. Descriptions and
# changelog entry bodies legitimately contain non-ASCII, and folding those would
# corrupt real content to satisfy a parser that never reads it.
#
# Diacritics are stripped via NFD decomposition rather than deleted, so Ondřej
# Surý becomes Ondrej Sury and not Ondej Sur. Anything still non-ASCII after
# that (CJK, for instance) has no ASCII form and is dropped.
use strict;
use warnings;
use Encode qw(decode encode FB_CROAK);
use Unicode::Normalize qw(NFD);

my $file = shift or die "usage: $0 debian/control\n";
open my $in, '<:raw', $file or die "$file: $!\n";
my @lines = <$in>;
close $in;

my $is_changelog = $file =~ m{(?:^|/)changelog$};
my $in_field = 0;
my $changed  = 0;

for my $l (@lines) {
    if ($is_changelog) {
        # Only the trailer carries the maintainer:
        #   " -- Name <email>  Thu, 01 Jan 2026 00:00:00 +0000"
        # Entry bodies are prose and are left exactly as written.
        $in_field = $l =~ /^ -- /;
    } elsif ($l =~ /^(?:Maintainer|Uploaders):/i) {
        $in_field = 1;
    } elsif ($in_field && $l =~ /^\s/) {
        # continuation of the field above
    } elsif ($l =~ /^\S/) {
        $in_field = 0;
    }
    next unless $in_field;
    next unless $l =~ /[^\x00-\x7f]/;          # already ASCII, leave alone

    my $dec = eval { decode('UTF-8', $l, FB_CROAK) };
    next unless defined $dec;                   # not valid UTF-8; do not guess

    my $out = NFD($dec);
    $out =~ s/\p{NonspacingMark}//g;            # drop combining accents
    $out =~ s/[^\x00-\x7f]//g;                  # and anything with no ASCII form
    my $bytes = encode('UTF-8', $out);
    if ($bytes ne $l) {
        $l = $bytes;
        $changed++;
    }
}

if ($changed) {
    open my $out, '>:raw', $file or die "$file: $!\n";
    print {$out} @lines;
    close $out;
    print "asciify-control: folded $changed line(s) in $file\n";
}
exit 0;
