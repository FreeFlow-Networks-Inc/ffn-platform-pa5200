#!/bin/bash
# ffn-nfsd -- NFSv3 root server for the Octeon control/data planes.
#
# The Octeon NFS-roots from the host: PAN's CP boot line is
#   bootoctlinux 21000000 ... rw nfsroot=/opt/dpfs,v3
#     ip=127.1.1.2:127.1.1.1:127.1.1.1:255.255.255.0:octeon:pci0
# so the host must be 127.1.1.1 and must export /opt/dpfs.
#
# Userspace comes from the owner's own nfs-utils in the PA-5220 image (the
# appliance has no nfs-utils and no internet); the kernel side is Ubuntu's
# nfsd. Those binaries are 2018-era and want libtirpc.so.1, which this box does
# not ship -- only .so.3 -- so the vendor library is referenced through
# /etc/ld.so.conf.d/ffn-vendor-nfs.conf. The library itself stays inside the
# vendor area so the never-package gates still cover it.
#
# SECURITY: exports are restricted to 127.1.0.0/16, exactly as PAN does. NFS
# listens on 0.0.0.0 but the bmfw input chain is policy-drop with a mgmt
# allow-list of 22/443/8443 only, so 111/2049/20048 are unreachable from the
# management network. Do NOT add them to mgmt_tcp_ports.
set -u
N=/var/lib/ffn-ngfw/vendor/gryphon-tools/nfs-server
B=$N/bin
export LD_LIBRARY_PATH=$N/lib
HOST_IP=127.1.1.1

have() { [ -x "$B/$1" ]; }

need_bins() {
    local missing=
    for f in exportfs rpc.nfsd rpc.mountd rpcbind; do
        have "$f" || missing="$missing $f"
    done
    if [ -n "$missing" ]; then
        echo "missing vendor nfs-utils:$missing"
        echo "copy them from the PA-5220 sysroot (usr/sbin, sbin) into $B"
        return 1
    fi
}

do_start() {
    need_bins || return 1

    mkdir -p /var/lib/nfs/{sm,sm.bak,v4recovery,rpc_pipefs}
    for f in etab rmtab xtab state; do [ -e /var/lib/nfs/$f ] || : > /var/lib/nfs/$f; done

    modprobe nfsd 2>/dev/null
    mountpoint -q /proc/fs/nfsd || mount -t nfsd nfsd /proc/fs/nfsd 2>/dev/null
    mountpoint -q /var/lib/nfs/rpc_pipefs || \
        mount -t rpc_pipefs sunrpc /var/lib/nfs/rpc_pipefs 2>/dev/null

    for d in /opt/dpfs /opt/var.cp /opt/var.dp0 /opt/var.dp1 /opt/var.dp2; do
        mkdir -p "$d"
    done

    if [ ! -s /etc/exports ] || ! grep -q '/opt/dpfs' /etc/exports; then
        cat > /etc/exports <<'EOF'
# FFN: NFS root for the Octeon control/data planes.
# 127.1.0.0/16 only -- the CP/DP address space, as PAN-OS restricts it.
/opt/dpfs    127.1.0.0/16(rw,sync,no_root_squash,no_subtree_check)
/opt/var.cp  127.1.0.0/16(rw,sync,no_root_squash,no_subtree_check)
/opt/var.dp0 127.1.0.0/16(rw,sync,no_root_squash,no_subtree_check)
/opt/var.dp1 127.1.0.0/16(rw,sync,no_root_squash,no_subtree_check)
/opt/var.dp2 127.1.0.0/16(rw,sync,no_root_squash,no_subtree_check)
EOF
    fi

    # the host must answer on 127.1.1.1. On lo until if_pci/pci0 exists.
    ip addr show dev lo | grep -q "$HOST_IP" || \
        ip addr add "$HOST_IP/32" dev lo 2>/dev/null

    ss -lnt 2>/dev/null | grep -q ':111 ' || { "$B/rpcbind" -w; sleep 1; }
    "$B/rpc.nfsd" --no-nfs-version 4 8 2>/dev/null; sleep 1
    "$B/exportfs" -r
    pgrep -f 'rpc.mountd' >/dev/null || { "$B/rpc.mountd" --no-nfs-version 4; sleep 1; }

    do_status
}

do_stop() {
    "$B/exportfs" -ua 2>/dev/null
    pkill -f 'rpc.mountd' 2>/dev/null
    "$B/rpc.nfsd" 0 2>/dev/null          # 0 threads = stop serving
    echo "stopped serving (rpcbind left running; it is harmless and shared)"
}

do_status() {
    echo "=== exports ==="
    "$B/exportfs" -v 2>/dev/null || echo "  exportfs unavailable"
    echo "=== nfsd threads ==="
    cat /proc/fs/nfsd/threads 2>/dev/null || echo "  nfsd not running"
    echo "=== listening ==="
    ss -lntu 2>/dev/null | grep -E ':(111|2049|20048)\b' || echo "  none"
    echo "=== host address for the Octeon ==="
    ip -br addr show lo | tr -s ' '
    echo "=== root filesystem being served ==="
    if [ -d /opt/dpfs/sbin ]; then
        printf '  /opt/dpfs  %s  %s files\n' \
            "$(du -sh /opt/dpfs 2>/dev/null | cut -f1)" \
            "$(find /opt/dpfs -xdev 2>/dev/null | wc -l)"
        ls -l /opt/dpfs/boot/vmlinux.oct2-dp 2>/dev/null | sed 's/^/  /'
    else
        echo "  /opt/dpfs is EMPTY -- copy the CP root from the 5220 image first"
    fi
    echo "=== exposure check ==="
    if grep -q '2049\|20048' /etc/ffn-ngfw/bmfw.json 2>/dev/null; then
        echo "  WARNING: an NFS port appears in bmfw.json -- NFS should NOT be"
        echo "  reachable from the management network"
    else
        echo "  ok: no NFS port in the mgmt allow-list (policy drop covers it)"
    fi
}

case "${1:-status}" in
    start)  do_start ;;
    stop)   do_stop ;;
    status) do_status ;;
    test)
        mkdir -p /mnt/nfstest
        if mount -t nfs -o vers=3,nolock "$HOST_IP:/opt/dpfs" /mnt/nfstest 2>&1; then
            echo "MOUNTED $HOST_IP:/opt/dpfs"
            ls /mnt/nfstest | tr '\n' ' '; echo
            umount /mnt/nfstest && echo "(unmounted)"
        else
            echo "mount failed -- is /sbin/mount.nfs linked to the vendor helper?"
        fi ;;
    *) echo "usage: $0 {start|stop|status|test}" ; exit 2 ;;
esac
