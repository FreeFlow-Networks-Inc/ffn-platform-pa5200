#!/bin/sh
# CP: run the retained OpenBCM stack with modules matching Debian's kernel.
set -eu
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
COMPAT=/opt/ffn-compat
MODULES=/usr/local/lib/ffn/modules
case "${1:-run}" in
prepare)
    # A command-line token alone does not prove the allocator excluded it.
    # The ring buffer can wrap during normal operation. The current boot's
    # kernel journal retains the allocator confirmation across service restarts.
    reservation='FFN: ffn_reserve 0x30000000+0x4000000 reserved, kept out of the allocator'
    if ! dmesg | grep -F "$reservation" >/dev/null &&
       ! journalctl --boot=0 --dmesg --no-pager --output=cat | grep -Fx "$reservation" >/dev/null; then
        echo 'BCM DMA pool is not reserved; fix boot arguments before loading BDE' >&2
        exit 1
    fi
    test -x "$COMPAT/usr/local/ffn/bcm.user.8481"
    test -f "$COMPAT/tmp/bcmcfg/config.bcm"
    test "$(readlink "$COMPAT/usr/share/broadcom")" = /tmp/bcmcfg
    THP=/sys/kernel/mm/transparent_hugepage
    if [ -w "$THP/enabled" ]; then
        echo never > "$THP/enabled"
        echo never > "$THP/defrag"
    fi
    grep -q '^ffn_bcm ' /proc/modules || /usr/bin/busybox insmod "$MODULES/ffn_bcm.ko"
    grep -q '^ffn_bde ' /proc/modules || /usr/bin/busybox insmod "$MODULES/ffn_bde.ko" dma_phys=0x30000000 dma_mb=64
    [ -c /dev/linux-kernel-bde ] || mknod /dev/linux-kernel-bde c 127 0
    [ -c /dev/linux-user-bde ] || mknod /dev/linux-user-bde c 126 0
    chmod 600 /dev/linux-kernel-bde /dev/linux-user-bde
    ;;
run)
    exec chroot "$COMPAT" /usr/bin/env PATH="$PATH" /usr/bin/python3 \
        /usr/local/ffn/ffn_bcmd.py --bcm /usr/local/ffn/bcm.user.8481 \
        --cfg /tmp/bcmcfg --bind 127.1.1.2
    ;;
*) echo 'usage: ffn-bcm-debian.sh {prepare|run}' >&2; exit 2 ;;
esac
