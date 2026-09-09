#!/bin/sh
# Build the iproute2 chain for mips64 BE, bottom-up, with build-deb-mips64.sh.
#
# RESULT: all 12 built. iproute2_7.2.0-1_mips64.deb, 21 binaries, every one
# "ELF 64-bit MSB pie executable, MIPS, MIPS64 rel2". ip/ss/tc/bridge all
# execute and ip makes real netlink calls.
#
# The chain is far shorter than the dependency lists suggest, because most of
# the expensive links were ALREADY built by the bootstrap. Measured, not
# assumed -- 30 of iproute2's 44 transitive build-deps were already present:
#
#   libelf-dev 0.196-1     present -> elfutils NOT needed, and elfutils was
#                          the only thing wanting gawk, which was the only
#                          thing wanting locales-all (a glibc rebuild)
#   libselinux-dev 3.11-2  present -> libselinux NOT needed
#   debhelper 14.3         provides debhelper-compat (= 14/13/12/11)
#
# TWO APT BEHAVIOURS THAT COST A PASS EACH, both worth not re-learning:
#
# 1. A pure VIRTUAL package reports "Candidate: (none)" even when it is
#    perfectly satisfiable. debhelper-compat is provided by debhelper;
#    perl-xs-dev is provided by libperl-dev. Checking availability with
#    apt-cache policy alone produces false "MISSING" reports -- use
#    apt-cache showpkg <pkg> and read Reverse Provides.
#
# 2. "Depends: X but it is not going to be installed" is often COLLATERAL,
#    not the cause. bison reported that for flex while flex, libfl2 and
#    libfl-dev all installed cleanly on their own; the real blocker was
#    help2man. Find the "not installable" line, not the "not going to be
#    installed" one.
#
# Order is dependency order. A failure does not stop the run: independent
# links still get their chance, and a dependent link failing right after its
# provider failed is itself the diagnosis.
set -u
BD=${BD:-$(dirname "$0")/build-deb-mips64.sh}
RESULT=${RESULT:-/tmp/iproute2-chain-result.txt}
: > "$RESULT"

# source|why it is in the chain
CHAIN='libmnl|libmnl-dev, needed by iproute2, libnftnl and libnetfilter-conntrack
liblocale-gettext-perl|help2man is blocked on this and nothing else; its own locales-all dep is <!nocheck>
help2man|the only thing blocking bison
bison|direct build-dep of iproute2
libtirpc|provides libtirpc-dev, which libnsl needs
libnsl|libnsl-dev, direct build-dep of iproute2
linux-atm|provides libatm1-dev, direct build-dep of iproute2
libnfnetlink|needed by libnetfilter-conntrack and iptables
libnftnl|needs libmnl-dev; needed by iptables
libnetfilter-conntrack|needs libmnl-dev + libnfnetlink-dev; needed by iptables
libbpf|libbpf-dev; its deps were already present
iptables|provides libxtables-dev, the last missing iproute2 build-dep
iproute2|the goal: ip, ss, bridge, tc'

echo "$CHAIN" | while IFS='|' read -r src why; do
	[ -z "$src" ] && continue
	echo
	echo "################################################################"
	echo "# $src"
	echo "#   $why"
	echo "################################################################"
	if sh "$BD" "$src"; then
		echo "$src OK" >> "$RESULT"
	else
		echo "$src FAILED" >> "$RESULT"
	fi
done

echo
echo "================ chain result ================"
cat "$RESULT"
echo "=============================================="
