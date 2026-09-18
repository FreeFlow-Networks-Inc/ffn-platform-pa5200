#!/bin/sh
# Build missing stock nft FIB expressions against the exact deployed DP kernel.
# No kernel tree edits, deployment, module loading or reboot occur here.
set -eu
kernel=${1:?usage: build-dataplane-netfilter.sh KERNEL_BUILD OUTPUT CROSS_PREFIX}
output=${2:?output directory required}
cross=${3:?MIPS64 big-endian cross prefix required}
test -s "$kernel/Module.symvers"
test -s "$kernel/include/config/kernel.release"
mkdir -p "$output"
output=$(realpath "$output")
cp "$kernel/net/netfilter/nft_fib.c" "$output/"
cp "$kernel/net/ipv4/netfilter/nft_fib_ipv4.c" "$output/"
cp "$kernel/net/ipv6/netfilter/nft_fib_ipv6.c" "$output/"
cp "$kernel/net/netfilter/nft_fib_inet.c" "$output/"
printf '%s\n' 'obj-m += nft_fib.o nft_fib_ipv4.o nft_fib_ipv6.o nft_fib_inet.o' > "$output/Makefile"
make -C "$kernel" M="$output" ARCH=mips CROSS_COMPILE="$cross" modules
cp "$kernel/include/config/kernel.release" "$output/kernel.release"
for module in "$output"/*.ko; do
    "${cross}readelf" -h "$module" | grep -q '2.s complement, big endian'
    "${cross}readelf" -h "$module" | grep -q 'ELF64'
done
(cd "$output" && sha256sum ./*.ko kernel.release > SHA256SUMS)
