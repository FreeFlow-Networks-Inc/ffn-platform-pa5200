#!/bin/sh
# FFN CP init for the upstream-6.18 forward port, first-boot verification.
#
# Deliberately self-reporting: this is the first boot of a kernel nobody has
# booted, over a serial console, with no network and no NFS. Every question we
# would otherwise have to log in and ask is printed here, so a boot that half
# works still tells us which half.
#
# It never exits. An init that returns is a panic.

/bin/busybox mkdir -p /proc /sys /dev /tmp 2>/dev/null
/bin/busybox mount -t proc  proc  /proc   2>/dev/null
/bin/busybox mount -t sysfs sysfs /sys    2>/dev/null
/bin/busybox mount -t devtmpfs devtmpfs /dev 2>/dev/null || \
	/bin/busybox mount -t tmpfs tmpfs /dev 2>/dev/null

say() { echo "FFN-INIT: $*"; }

echo
echo "================ FFN CP / upstream 6.18 forward port ================"
say "uname:   $(/bin/busybox uname -srm 2>/dev/null)"
say "cmdline: $(/bin/busybox cat /proc/cmdline 2>/dev/null)"

# --- memory: is ffn_mem=auto really unnecessary on 6.18? -------------------
say "--- memory (upstream defaults max_memory to ULLONG_MAX; no mem= passed) ---"
/bin/busybox grep -E "MemTotal|MemFree" /proc/meminfo 2>/dev/null | while read -r l; do say "  $l"; done
say "  cores online: $(/bin/busybox grep -c ^processor /proc/cpuinfo 2>/dev/null)"

# --- did ffn_reserve= actually punch holes? -------------------------------
say "--- System RAM ranges (holes = ffn_reserve worked) ---"
/bin/busybox grep "System RAM" /proc/iomem 2>/dev/null | while read -r l; do say "  $l"; done

# --- the transport regions, by the corrected signature table --------------
# 0x400=KPF_BUDDY (free, DANGEROUS), 0x0=managed+allocated OR excluded,
# 0x800=no struct page (below floor), 0x100000=KPF_NOPAGE (unmanaged).
# /proc/iomem above is authoritative; this is corroboration.
say "--- kpageflags at the transport bases ---"
PS=$(/bin/busybox getconf PAGE_SIZE 2>/dev/null || echo 4096)
for a in 0x22000000 0x28000000 0x29000000 0x21000000; do
	pfn=$(( a / PS ))
	v=$(/bin/busybox dd if=/proc/kpageflags bs=8 skip=$pfn count=1 2>/dev/null \
		| /bin/busybox od -An -tx8 | /bin/busybox tr -d ' ')
	say "  $a flags=0x$v"
done

# --- did the FDT come from the __fdt named block? -------------------------
say "--- device tree source (want: found by name) ---"
/bin/busybox dmesg 2>/dev/null | /bin/busybox grep -iE "FFN:|Device Tree" | while read -r l; do say "  $l"; done

# --- CIU3 from a stock upstream build ------------------------------------
say "--- interrupts (CIU3 = upstream irqchip is driving this board) ---"
/bin/busybox grep -iE "CIU" /proc/interrupts 2>/dev/null | /bin/busybox head -4 | while read -r l; do say "  $l"; done

say "--- net (expect NONE: octeon3-ethernet is tranche 4) ---"
say "  $(/bin/busybox ls /sys/class/net 2>/dev/null | /bin/busybox tr '\n' ' ')"

echo "===================================================================="
say "verification complete; dropping to a shell on the console"
echo

exec /bin/busybox sh -i
