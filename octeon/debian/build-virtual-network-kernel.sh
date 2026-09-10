#!/bin/sh
# VM: isolated successor to the tested DP network kernel; no deployment here.
set -eu
BASE=${BASE:-/mnt/clones/fwdport/debian-candidates}
VARIANT=${VARIANT:-virtual}
case "$VARIANT" in virtual|router) ;; *) echo 'invalid kernel variant' >&2; exit 2 ;; esac
TREE=$BASE/linux-dp-$VARIANT
SOURCE_TREE=${SOURCE_TREE:-$BASE/linux-dp-network}
CROSS=/mnt/clones/fwdport/gcc-14.4.0-nolibc/mips64-linux/bin/mips64-linux-
exec 9>"$BASE/virtual-network-build.lock"
flock -n 9
test -d "$TREE" || cp -a --reflink=auto "$SOURCE_TREE" "$TREE"
cd "$TREE"
for symbol in NET_SCHED NET_CLS NET_CLS_ACT NET_SWITCHDEV \
    NF_CONNTRACK_MARK NF_CONNTRACK_ZONES NF_CONNTRACK_EVENTS NF_CONNTRACK_LABELS \
    NET_IPGRE_BROADCAST IP_MULTIPLE_TABLES IPV6_MULTIPLE_TABLES NET_L3_MASTER_DEV; do
    scripts/config --enable "$symbol"
done
for symbol in MACSEC VXLAN GENEVE NET_IPGRE_DEMUX NET_IPGRE IPV6_GRE \
    OPENVSWITCH OPENVSWITCH_GRE OPENVSWITCH_VXLAN OPENVSWITCH_GENEVE \
    NET_SCH_INGRESS NET_CLS_FLOWER NET_ACT_MIRRED NET_ACT_GACT NET_ACT_VLAN \
    NET_ACT_TUNNEL_KEY NET_ACT_PEDIT NET_ACT_CSUM NET_ACT_CT NET_ACT_POLICE \
    NET_ACT_SAMPLE PSAMPLE NET_VRF BONDING DUMMY XFRM_USER XFRM_INTERFACE \
    INET_ESP INET6_ESP NET_IPIP; do
    scripts/config --module "$symbol"
done
scripts/config --set-str LOCALVERSION "-ffn-debian-dp-$VARIANT" --disable LOCALVERSION_AUTO
make ARCH=mips CROSS_COMPILE="$CROSS" olddefconfig
for symbol in MACSEC VXLAN GENEVE NET_IPGRE OPENVSWITCH NET_CLS_FLOWER NET_VRF; do
    grep -qx "CONFIG_$symbol=m" .config
done
grep -qx 'CONFIG_NET_L3_MASTER_DEV=y' .config
make -j8 ARCH=mips CROSS_COMPILE="$CROSS" vmlinux modules
"${CROSS}strip" -o "$BASE/ffn-vmlinux-systemd-dp-$VARIANT" vmlinux
cp .config "$BASE/config-dp-$VARIANT"
make ARCH=mips CROSS_COMPILE="$CROSS" INSTALL_MOD_PATH="$BASE/modules-dp-$VARIANT" modules_install
cd "$BASE"
sha256sum "ffn-vmlinux-systemd-dp-$VARIANT" > "ffn-vmlinux-systemd-dp-$VARIANT.sha256"
