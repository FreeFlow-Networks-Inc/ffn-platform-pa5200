#!/bin/sh
# CP: run the native OpenBCM executable with modules matching Debian's kernel.
set -eu
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
FFN=/usr/local/ffn
CFG=/usr/share/broadcom
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
    test -x "$FFN/bcm.user"
    test -f "$CFG/config.bcm"
    # The owner's reviewed config/firmware tree must survive tmpfs cleanup.
    case "$(readlink -f "$CFG")" in
        /tmp/*|/run/*) echo 'BCM configuration must be provisioned on persistent storage' >&2; exit 1 ;;
    esac
    # External microcode method 2 silently leaves the PA-5220 SerDes down.
    python3 - "$CFG/config.bcm" <<'PY'
import re, sys
from pathlib import Path
values = re.findall(r'^\s*load_firmware\.BCM88650\s*=\s*(0x[0-9a-fA-F]+|[0-9]+)\s*(?:#.*)?$', Path(sys.argv[1]).read_text(), re.M)
if not values or int(values[-1], 0) != 1:
    raise SystemExit('PA-5220 requires reviewed internal SerDes firmware loading (load_firmware.BCM88650=0x1)')
PY
    THP=/sys/kernel/mm/transparent_hugepage
    if [ -w "$THP/enabled" ]; then
        echo never > "$THP/enabled"
        echo never > "$THP/defrag"
    fi
    /usr/sbin/modprobe ffn_bcm
    /usr/sbin/modprobe ffn_bde dma_phys=0x30000000 dma_mb=64
    [ -c /dev/linux-kernel-bde ] || mknod /dev/linux-kernel-bde c 127 0
    [ -c /dev/linux-user-bde ] || mknod /dev/linux-user-bde c 126 0
    chmod 600 /dev/linux-kernel-bde /dev/linux-user-bde
    ;;
run)
    exec /usr/bin/python3 "$FFN/ffn_bcmd.py" --bcm "$FFN/bcm.user" \
        --cfg "$CFG" --bind 127.1.1.2
    ;;
*) echo 'usage: ffn-bcm-debian.sh {prepare|run}' >&2; exit 2 ;;
esac
