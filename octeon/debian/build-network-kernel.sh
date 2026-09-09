#!/bin/sh
# VM: isolated DP kernel with the Linux L2/L3 software datapath.
set -eu
BASE=${BASE:-/mnt/clones/fwdport/debian-candidates}
TREE=$BASE/linux-dp-network
CROSS=/mnt/clones/fwdport/gcc-14.4.0-nolibc/mips64-linux/bin/mips64-linux-
exec 9>"$BASE/network-build.lock"
flock -n 9
test -d "$TREE" || cp -a --reflink=auto "$BASE/linux-dp" "$TREE"
cd "$TREE"
for symbol in BRIDGE BRIDGE_VLAN_FILTERING VLAN_8021Q VETH NETFILTER NETFILTER_ADVANCED \
    NF_TABLES NF_TABLES_INET NF_CONNTRACK NF_NAT NFT_CT NFT_COUNTER \
    NFT_LOG NFT_LIMIT NFT_REJECT NFT_REJECT_INET NFT_NAT NFT_MASQ \
    IP_ADVANCED_ROUTER IP_MULTIPLE_TABLES IPV6_MULTIPLE_TABLES; do
    scripts/config --enable "$symbol"
done
scripts/config --set-str LOCALVERSION '-ffn-debian-dp-network' --disable LOCALVERSION_AUTO
make ARCH=mips CROSS_COMPILE="$CROSS" olddefconfig
for symbol in BRIDGE BRIDGE_VLAN_FILTERING VLAN_8021Q VETH NETFILTER NF_TABLES IP_MULTIPLE_TABLES; do
    grep -qx "CONFIG_$symbol=y" .config
done
make -j8 ARCH=mips CROSS_COMPILE="$CROSS" vmlinux modules
"${CROSS}strip" -o "$BASE/ffn-vmlinux-systemd-dp-network" vmlinux
cp .config "$BASE/config-dp-network"
make ARCH=mips CROSS_COMPILE="$CROSS" INSTALL_MOD_PATH="$BASE/modules-dp-network" modules_install
cd "$BASE"
sha256sum ffn-vmlinux-systemd-dp-network > ffn-vmlinux-systemd-dp-network.sha256
